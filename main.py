"""
main.py — Ultra-Conservative Gold Futures Trading Bot  (v3 — Central Control)
==============================================================================
Main orchestrator: initialises all components, receives TradingView webhook
signals AND Telegram signals, validates them through a 12-gate pipeline,
sizes positions, places bracket orders, and monitors risk in real time.

v3 changes (on top of v2):
  • Integrates BotState singleton — shared state across all components
  • Launches DashboardServer (port 8088) — real-time web control center
  • Launches TelegramReceiver — long-polls @Quant_PF_signals_bot for signals
    and operator commands (/pause /resume /flatten /stop /risk …)
  • Drains command_queue each monitor cycle (dashboard + Telegram → bot)
  • Pushes all runtime state to BotState for live dashboard/WS updates

Run:
    python main.py                  # demo/paper mode (safe default)
    DEMO_MODE=false python main.py  # live trading (needs real credentials)

Emergency stop:
    Ctrl+C  |  SIGTERM  |  POST /flatten with webhook secret  |  Dashboard  |  Telegram /stop
"""

import asyncio
import logging
import math
import os
import signal as _signal
import sys
from datetime import date, datetime
from typing import Optional

import pytz

import config
from tradovate_client   import TradovateClient
from risk_manager       import RiskManager
from strategy_logic     import StrategyEngine
from news_filter        import NewsFilter
from telegram_alerts    import TelegramAlerter
from webhook_server     import WebhookServer
from order_flow_filter  import OrderFlowFilter
from bot_state          import BotState
from dashboard_server   import DashboardServer
from telegram_receiver  import TelegramReceiver

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════════════
os.makedirs("logs",                   exist_ok=True)
os.makedirs(config.DAILY_REPORT_DIR,  exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


# ── Custom log handler to mirror log lines into BotState ────────────────────
class _StateLogHandler(logging.Handler):
    """Forwards log records into the BotState live-log buffer for the dashboard."""

    def __init__(self, state: BotState) -> None:
        super().__init__()
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._state.add_log(record.levelname, self.format(record).split("] ", 1)[-1])
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# TRADING BOT
# ═══════════════════════════════════════════════════════════════════════════════
class TradingBot:
    """
    Central orchestrator.

    Lifecycle:
        TradingBot() → initialize() → run()
                                         ↕  (signal loop + monitor loop + dashboard + tg receiver)
                                       shutdown()
    """

    def __init__(self) -> None:
        self.tz          = config.TIMEZONE
        self.tradovate   = TradovateClient()
        self.risk        = RiskManager()
        self.strategy    = StrategyEngine()
        self.news        = NewsFilter()
        self.telegram    = TelegramAlerter()
        self.webhook     = WebhookServer(on_signal=self._on_signal_received)

        # ── Shared state (singleton) ──────────────────────────────────────
        self.state: BotState = BotState.get()
        self.state.bot_ref   = self          # back-reference for dashboard controls

        # ── Dashboard server ──────────────────────────────────────────────
        self.dashboard = DashboardServer(self.state)

        # ── Telegram receiver (signal + command input) ────────────────────
        self.tg_receiver = TelegramReceiver(on_signal=self._on_signal_received)

        self.account_id: Optional[int]   = None
        self.running:    bool             = False
        self._shutdown_evt                = asyncio.Event()
        self._last_daily_reset:  str      = ""
        self._receiver_task: Optional[asyncio.Task] = None

        # OF filter stats (for daily summary)
        self._of_signals_total: int  = 0
        self._of_signals_pass:  int  = 0

        of_status = "ENABLED" if config.ORDER_FLOW_ENABLED else "disabled"
        mode      = "🔴 LIVE" if not config.DEMO_MODE else "🟡 DEMO/PAPER"
        startup_msg = (
            "=" * 60
            + f"\n  UltimateTradingBot v3 starting …"
            + f"\n  Mode: {mode}"
            + f"\n  Instrument: {config.DEFAULT_INSTRUMENT}"
            + f"\n  Risk/trade: {config.RISK_PER_TRADE_PCT * 100:.2f}%"
            + f"\n  Daily loss limit: {config.DAILY_LOSS_LIMIT_PCT * 100:.2f}%"
            + f"\n  Order Flow filter: {of_status}"
            + f"\n  Prop firm: {config.PROP_FIRM}"
            + f"\n  Dashboard: http://0.0.0.0:{os.getenv('DASHBOARD_PORT', '8088')}"
            + "\n" + "=" * 60
        )
        logger.info(startup_msg)

        # Attach state log handler so all log lines reach the dashboard
        _handler = _StateLogHandler(self.state)
        _handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logging.getLogger().addHandler(_handler)

    # ══════════════════════════════════════════════════════════════════════════
    # INITIALISATION
    # ══════════════════════════════════════════════════════════════════════════
    async def initialize(self) -> None:
        logger.info("Initialising …")

        await self.tradovate.authenticate()
        accounts = await self.tradovate.get_accounts()
        if not accounts:
            raise RuntimeError("No Tradovate accounts found. Check credentials.")

        self.account_id = accounts[0]["id"]
        equity = await self.tradovate.get_account_equity(self.account_id)
        logger.info("Account %d | Equity: $%.2f", self.account_id, equity)

        self.risk.initialize(equity)

        try:
            await self.tradovate.connect_ws()
        except Exception as exc:
            logger.warning("WebSocket unavailable — running REST-only: %s", exc)

        await self.news.fetch_calendar()

        # Push initial news state
        _now = datetime.now(self.tz)
        _ttne = self.news.time_to_next_event(_now)
        self.state.update_news(
            events    = self.news.get_today_events(),
            blackout  = self.news.is_blackout_window(_now),
            next_min  = int(_ttne.total_seconds() / 60) if _ttne else None,
        )

        # Initialise shared state
        self.state.set_running(True)
        self.state.account_id        = self.account_id
        self.state.demo_mode         = config.DEMO_MODE
        self.state.instrument        = config.DEFAULT_INSTRUMENT
        self.state.prop_firm         = config.PROP_FIRM
        self.state.is_paused         = False
        self.state.is_shutdown_today = False

        # Push initial equity state
        self.state.update_equity(
            equity        = equity,
            session_start = equity,
            high_water    = equity,
        )

        # Start webhook (TradingView) and dashboard
        self.webhook.start()
        self.dashboard.start()

        # Start Telegram receiver as background task
        self._receiver_task = asyncio.create_task(
            self.tg_receiver.run(), name="tg_receiver"
        )

        mode_label = "🔴 LIVE" if not config.DEMO_MODE else "🟡 DEMO/PAPER"
        await self.telegram.send_startup(
            mode       = mode_label,
            equity     = equity,
            instrument = config.DEFAULT_INSTRUMENT,
            risk_pct   = config.RISK_PER_TRADE_PCT * 100,
        )

        self.running = True
        logger.info("✅ Initialisation complete — awaiting signals …")

    # ══════════════════════════════════════════════════════════════════════════
    # COMMAND QUEUE DRAINER  (dashboard + Telegram → trading loop)
    # ══════════════════════════════════════════════════════════════════════════
    async def _drain_command_queue(self) -> None:
        """
        Drains every pending command from the shared command_queue each monitor
        cycle.  Commands originate from:
          • Dashboard REST controls (/api/control/*)
          • Telegram receiver (/pause /resume /flatten /stop)
        """
        while not self.state.command_queue.empty():
            try:
                cmd = self.state.command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            command = cmd.get("_command", "")
            secret  = cmd.get("secret", "")

            # Validate secret for security (commands from dashboard already
            # carry the webhook secret injected by the dashboard server).
            if secret != config.WEBHOOK_SECRET:
                logger.warning("Command rejected — bad secret: %s", command)
                continue

            if command == "pause":
                self.risk.is_paused      = True
                self.state.is_paused     = True
                logger.info("⏸ Bot PAUSED via control channel")
                await self.telegram.send_pause_alert(
                    "Manual pause via dashboard/Telegram", self.risk.get_daily_stats()
                )

            elif command == "resume":
                self.risk.is_paused      = False
                self.state.is_paused     = False
                logger.info("▶️ Bot RESUMED via control channel")
                await self.telegram.send_custom("▶️ Bot RESUMED — accepting new signals.")

            elif command == "flatten":
                await self._flatten_all("Operator emergency flatten via dashboard/Telegram")

            elif command == "stop":
                logger.info("🛑 Stop command received via control channel")
                self.stop()

            else:
                logger.debug("Unknown command in queue: %s", command)

    # ══════════════════════════════════════════════════════════════════════════
    # SIGNAL HANDLER  (entry point for both TradingView and Telegram signals)
    # ══════════════════════════════════════════════════════════════════════════
    async def _on_signal_received(self, payload: dict) -> None:
        command = payload.get("_command")
        if command == "flatten":
            await self._flatten_all("Operator emergency flatten")
            return
        if command == "pause":
            self.risk.is_paused  = True
            self.state.is_paused = True
            await self.telegram.send_pause_alert(
                "Manual pause via webhook", self.risk.get_daily_stats()
            )
            return
        if command == "resume":
            self.risk.is_paused  = False
            self.state.is_paused = False
            await self.telegram.send_custom("▶️ Bot RESUMED — accepting new signals.")
            return
        if command == "stop":
            self.stop()
            return

        try:
            await self._process_trading_signal(payload)
        except Exception as exc:
            logger.error("Error processing signal: %s", exc, exc_info=True)
            await self.telegram.send_error(f"Signal processing error: {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # 12-GATE SIGNAL PIPELINE
    # ══════════════════════════════════════════════════════════════════════════
    async def _process_trading_signal(self, payload: dict) -> None:
        now = datetime.now(self.tz)

        # ── Gate 1: Session window ────────────────────────────────────────
        if not self.strategy.is_trading_session(now):
            logger.info("Gate 1 FAIL: outside trading session (%s ET)", now.strftime("%H:%M"))
            self.state.add_signal_log(
                action      = payload.get("action", "?"),
                instrument  = payload.get("instrument", config.DEFAULT_INSTRUMENT),
                signal_type = payload.get("signal_type", "ORB"),
                price       = float(payload.get("price", 0)),
                passed      = False,
                gate        = "Gate1:SessionTime",
            )
            return

        # ── Gate 2: Entry allowed (past OR period) ────────────────────────
        if not self.strategy.is_entry_allowed(now):
            logger.info("Gate 2 FAIL: entry not yet allowed (OR window still open)")
            self.state.add_signal_log(
                action      = payload.get("action", "?"),
                instrument  = payload.get("instrument", config.DEFAULT_INSTRUMENT),
                signal_type = payload.get("signal_type", "ORB"),
                price       = float(payload.get("price", 0)),
                passed      = False,
                gate        = "Gate2:ORWindow",
            )
            return

        # ── Gate 3: Bot not paused / shutdown ────────────────────────────
        if self.risk.is_paused:
            logger.info("Gate 3 FAIL: bot is paused")
            return
        if self.risk.is_shutdown_today:
            logger.info("Gate 3 FAIL: daily shutdown active")
            return

        # ── Gate 4: Refresh equity + daily-loss check ─────────────────────
        equity = await self.tradovate.get_account_equity(self.account_id)
        self.risk.update_equity(equity)

        # Push updated equity to shared state
        self.state.update_equity(
            equity        = equity,
            session_start = self.risk.session_start_equity,
            high_water    = self.risk.high_water_equity,
        )

        if self.risk.is_daily_loss_limit_hit():
            self.risk.is_shutdown_today      = True
            self.state.is_shutdown_today     = True
            await self.telegram.send_daily_loss_limit_alert(self.risk.get_daily_stats())
            await self._flatten_all("Daily loss limit")
            return

        # ── Gate 5: Daily profit target ───────────────────────────────────
        if self.risk.is_daily_profit_target_hit():
            logger.info(
                "Gate 5: Daily profit target reached (%.2f%%). No new trades.",
                config.DAILY_PROFIT_TARGET_PCT * 100,
            )
            return

        # ── Gate 6: Max trades per day ────────────────────────────────────
        if self.risk.daily_trades >= config.MAX_TRADES_PER_DAY:
            logger.info("Gate 6 FAIL: max trades/day (%d) reached", config.MAX_TRADES_PER_DAY)
            return

        # ── Gate 7: Consecutive-loss stop ─────────────────────────────────
        if self.risk.consecutive_losses >= 2 and config.AFTER_2_LOSSES_STOP:
            logger.info(
                "Gate 7 FAIL: %d consecutive losses — stopped for today",
                self.risk.consecutive_losses,
            )
            return

        # ── Gate 8: News blackout ─────────────────────────────────────────
        if self.news.is_blackout_window(now):
            logger.info("Gate 8 FAIL: news blackout window")
            self.state.add_signal_log(
                action      = payload.get("action", "?"),
                instrument  = payload.get("instrument", config.DEFAULT_INSTRUMENT),
                signal_type = payload.get("signal_type", "ORB"),
                price       = float(payload.get("price", 0)),
                passed      = False,
                gate        = "Gate8:NewsBlackout",
            )
            return

        # ── Gate 9: No existing position ─────────────────────────────────
        positions = await self.tradovate.get_positions(self.account_id)
        self.state.open_positions = positions
        if positions:
            logger.info("Gate 9 FAIL: position already open")
            return

        # ── Gate 10: Strategy validation (includes Order Flow gate) ───────
        if config.ORDER_FLOW_ENABLED:
            self._of_signals_total      += 1
            self.state.of_signals_total += 1

        signal = self.strategy.parse_and_validate_signal(payload)
        if signal is None:
            logger.info("Gate 10 FAIL: strategy / order flow validation failed")
            self.state.add_signal_log(
                action      = payload.get("action", "?"),
                instrument  = payload.get("instrument", config.DEFAULT_INSTRUMENT),
                signal_type = payload.get("signal_type", "ORB"),
                price       = float(payload.get("price", 0)),
                passed      = False,
                gate        = "Gate10:Strategy/OF",
            )
            return

        of_result = signal.get("of_result")
        if config.ORDER_FLOW_ENABLED and of_result and of_result.passed:
            self._of_signals_pass      += 1
            self.state.of_signals_pass += 1

        instrument = signal["instrument"]

        # ── Gate 11: Position sizing ───────────────────────────────────────
        inst_spec = config.INSTRUMENTS[instrument]
        sl_pts    = signal["sl_points"]

        sizing = self.risk.calculate_position_size(
            equity             = equity,
            sl_points          = sl_pts,
            point_value        = inst_spec.point_value,
            consecutive_losses = self.risk.consecutive_losses,
        )

        contracts = int(sizing["contracts"])
        if contracts < 1:
            logger.warning("Gate 11 FAIL: calculated 0 contracts — skipping")
            self.state.add_signal_log(
                action      = signal["action"],
                instrument  = instrument,
                signal_type = signal.get("signal_type", "ORB"),
                price       = signal["price"],
                passed      = False,
                gate        = "Gate11:PositionSize",
                of_summary  = signal.get("of_summary", ""),
            )
            return

        # ── Gate 12: Consistency rule ─────────────────────────────────────
        potential_gain = sizing["risk_amount"] * config.TP1_R
        if self.risk.would_violate_consistency_rule(potential_gain):
            logger.info("Gate 12 FAIL: consistency rule — no day > 35%% of total profits")
            return

        # ── Compute SL / TP levels ────────────────────────────────────────
        action = signal["action"]
        entry  = signal["price"]
        levels = self.strategy.compute_levels(action, entry, sl_pts)

        contracts_tp1 = max(1, contracts // 2)
        contracts_tp2 = contracts - contracts_tp1

        logger.info(
            "🎯 %s %d×%s @ %.2f | SL=%.2f TP1=%.2f (×%d) TP2=%.2f (×%d) | "
            "Risk=$%.2f (%.3f%%) | %s",
            action, contracts, instrument, entry,
            levels["sl_price"], levels["tp1_price"], contracts_tp1,
            levels["tp2_price"], contracts_tp2,
            sizing["risk_amount"], sizing["risk_pct"],
            signal.get("of_summary", "OF:N/A"),
        )

        # ── Place bracket order ───────────────────────────────────────────
        tradovate_action = "Buy" if action == "BUY" else "Sell"
        order_result = await self.tradovate.place_bracket_order(
            account_id    = self.account_id,
            symbol        = instrument,
            action        = tradovate_action,
            contracts     = contracts,
            sl_price      = levels["sl_price"],
            tp1_price     = levels["tp1_price"],
            tp2_price     = levels["tp2_price"],
            contracts_tp1 = contracts_tp1,
            contracts_tp2 = contracts_tp2,
        )

        if order_result is None or order_result.get("failureReason"):
            reason = (order_result or {}).get("failureReason", "No response")
            logger.error("❌ Order rejected: %s", reason)
            await self.telegram.send_error(f"Order rejected: {reason}")
            return

        # ── Record & alert ────────────────────────────────────────────────
        trade_info = {
            "action":          action,
            "instrument":      instrument,
            "contracts":       contracts,
            "entry_price":     entry,
            "sl_price":        levels["sl_price"],
            "tp1_price":       levels["tp1_price"],
            "tp2_price":       levels["tp2_price"],
            "risk_amount":     sizing["risk_amount"],
            "risk_pct":        sizing["risk_pct"],
            "entry_time":      now.isoformat(),
            "order_id":        str(order_result.get("orderId", "")),
            "equity_at_entry": equity,
            "signal_type":     signal.get("signal_type", "ORB"),
            "of_summary":      signal.get("of_summary", "OF:N/A"),
            "status":          "open",
        }
        self.risk.record_trade_entry(trade_info)

        # Push open trade to shared state
        self.state.record_open_trade(trade_info)
        self.state.daily_trades       = self.risk.daily_trades
        self.state.consecutive_losses = self.risk.consecutive_losses

        # Log passing signal
        self.state.add_signal_log(
            action      = action,
            instrument  = instrument,
            signal_type = signal.get("signal_type", "ORB"),
            price       = entry,
            passed      = True,
            gate        = "ALL_PASS",
            of_summary  = signal.get("of_summary", ""),
            source      = payload.get("source", "tradingview"),
        )

        await self.telegram.send_entry_alert(
            trade       = trade_info,
            equity      = equity,
            daily_stats = self.risk.get_daily_stats(),
            of_result   = of_result,
        )
        logger.info("✅ Order placed | orderId=%s", order_result.get("orderId"))

    # ══════════════════════════════════════════════════════════════════════════
    # POSITION MONITOR
    # ══════════════════════════════════════════════════════════════════════════
    async def _monitor_loop(self) -> None:
        while self.running:
            try:
                # ── Drain command queue first ─────────────────────────────
                await self._drain_command_queue()

                now    = datetime.now(self.tz)
                equity = await self.tradovate.get_account_equity(self.account_id)
                self.risk.update_equity(equity)

                # Push equity + risk metrics to shared state
                self.state.update_equity(
                    equity        = equity,
                    session_start = self.risk.session_start_equity,
                    high_water    = self.risk.high_water_equity,
                )
                self.state.daily_trades       = self.risk.daily_trades
                self.state.winning_trades     = self.risk.winning_trades
                self.state.losing_trades      = self.risk.losing_trades
                self.state.consecutive_losses = self.risk.consecutive_losses
                self.state.is_paused          = self.risk.is_paused
                self.state.is_shutdown_today  = self.risk.is_shutdown_today

                # Sync OF stats
                self.state.of_signals_total = self._of_signals_total
                self.state.of_signals_pass  = self._of_signals_pass

                # ── Fetch and push open positions ─────────────────────────
                try:
                    positions = await self.tradovate.get_positions(self.account_id)
                    self.state.open_positions = positions
                except Exception:
                    pass

                # ── Refresh news state ────────────────────────────────────
                _ttne = self.news.time_to_next_event(now)
                self.state.update_news(
                    events    = self.news.get_today_events(),
                    blackout  = self.news.is_blackout_window(now),
                    next_min  = int(_ttne.total_seconds() / 60) if _ttne else None,
                )

                # ── EOD time stop ─────────────────────────────────────────
                if self.strategy.is_force_close_time(now):
                    positions = await self.tradovate.get_positions(self.account_id)
                    if positions:
                        logger.info("⏰ Time stop: force-closing all positions")
                        await self._flatten_all("Time stop — 14:45 ET")
                        self.risk.record_trade_exit(exit_price=0, pnl=0, status="time_stop")
                        self.state.record_trade_closed(
                            {**(self.state.open_trade or {}), "status": "time_stop"}
                        )

                # ── Daily loss re-check ───────────────────────────────────
                if not self.risk.is_shutdown_today and self.risk.is_daily_loss_limit_hit():
                    self.risk.is_shutdown_today  = True
                    self.state.is_shutdown_today = True
                    await self._flatten_all("Daily loss limit hit")
                    await self.telegram.send_daily_loss_limit_alert(self.risk.get_daily_stats())

                # ── Intraday drawdown pause ───────────────────────────────
                if not self.risk.is_paused and self.risk.is_intraday_pause_triggered():
                    self.risk.is_paused  = True
                    self.state.is_paused = True
                    logger.warning("⚠️ Intraday DD pause triggered")
                    await self.telegram.send_pause_alert(
                        reason      = f"Intraday drawdown > {config.INTRADAY_DD_PAUSE_PCT*100:.1f}%",
                        daily_stats = self.risk.get_daily_stats(),
                    )

                # ── Daily reset at 09:00 ET ───────────────────────────────
                today = now.date().isoformat()
                if now.hour == 9 and now.minute < 15 and today != self._last_daily_reset:
                    logger.info("📅 Performing daily reset …")
                    self.risk.reset_daily()
                    self._of_signals_total       = 0
                    self._of_signals_pass        = 0
                    self.state.daily_trades      = 0
                    self.state.winning_trades    = 0
                    self.state.losing_trades     = 0
                    self.state.consecutive_losses = 0
                    self.state.is_shutdown_today  = False
                    self.state.of_signals_total  = 0
                    self.state.of_signals_pass   = 0
                    await self.news.fetch_calendar()
                    self._last_daily_reset = today

                # ── EOD daily summary at 15:05 ET ─────────────────────────
                if now.hour == 15 and now.minute == 5:
                    stats = self.risk.get_daily_stats()
                    stats["of_filter_total"]  = self._of_signals_total
                    stats["of_filter_passes"] = self._of_signals_pass
                    await self.telegram.send_daily_summary(stats)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Monitor loop error: %s", exc, exc_info=True)

            await asyncio.sleep(config.POSITION_POLL_INTERVAL)

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════════
    async def _flatten_all(self, reason: str = "Emergency") -> None:
        logger.warning("🚨 FLATTEN ALL — %s", reason)
        if self.account_id:
            await self.tradovate.flatten_all(self.account_id)
        await self.telegram.send_flatten_alert(reason)
        # Clear open trade in shared state
        if self.state.open_trade:
            self.state.record_trade_closed(
                {**(self.state.open_trade), "status": "flattened", "reason": reason}
            )
        self.state.open_positions = []

    # ══════════════════════════════════════════════════════════════════════════
    # RUN / SHUTDOWN
    # ══════════════════════════════════════════════════════════════════════════
    async def run(self) -> None:
        await self.initialize()
        monitor_task = asyncio.create_task(self._monitor_loop(), name="monitor")
        logger.info(
            "🚀 Bot is live — awaiting signals on webhook & Telegram | "
            "Dashboard: http://0.0.0.0:%s",
            os.getenv("DASHBOARD_PORT", "8088"),
        )
        try:
            await self._shutdown_evt.wait()
        except asyncio.CancelledError:
            pass
        finally:
            monitor_task.cancel()
            if self._receiver_task and not self._receiver_task.done():
                self._receiver_task.cancel()
                try:
                    await self._receiver_task
                except asyncio.CancelledError:
                    pass
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Shutting down …")
        self.running = False
        self.state.set_running(False)

        if self.account_id:
            positions = await self.tradovate.get_positions(self.account_id)
            if positions:
                await self._flatten_all("Bot shutdown")

        await self.telegram.send_shutdown(self.risk.get_daily_stats())
        self.webhook.stop()
        self.dashboard.stop()
        await self.tradovate.close()
        await self.telegram.close()
        logger.info("✅ Shutdown complete.")

    def stop(self) -> None:
        self._shutdown_evt.set()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
async def _amain() -> None:
    bot  = TradingBot()
    loop = asyncio.get_running_loop()

    def _handle_signal(sig: int) -> None:
        logger.info("Signal %s received — stopping …", sig)
        bot.stop()

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    await bot.run()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — exiting.")
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
