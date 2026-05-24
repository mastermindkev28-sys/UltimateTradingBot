"""
main.py — Ultra-Conservative Gold Futures Trading Bot  (v2 — with Order Flow)
==============================================================================
Main orchestrator: initialises all components, receives TradingView webhook
signals, validates them through a 12-gate pipeline, sizes positions, places
bracket orders, and monitors risk in real time.

v2 changes:
  • Passes of_result from strategy_logic to telegram_alerts.send_entry_alert
  • Logs OF summary in the trade execution line
  • OF filter statistics tracked in daily stats

Run:
    python main.py                  # demo/paper mode (safe default)
    DEMO_MODE=false python main.py  # live trading (needs real credentials)

Emergency stop:
    Ctrl+C  |  SIGTERM  |  POST /flatten with webhook secret
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
from tradovate_client import TradovateClient
from risk_manager      import RiskManager
from strategy_logic    import StrategyEngine
from news_filter       import NewsFilter
from telegram_alerts   import TelegramAlerter
from webhook_server    import WebhookServer
from order_flow_filter import OrderFlowFilter

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


# ═══════════════════════════════════════════════════════════════════════════════
# TRADING BOT
# ═══════════════════════════════════════════════════════════════════════════════
class TradingBot:
    """
    Central orchestrator.

    Lifecycle:
        TradingBot() → initialize() → run()
                                         ↕  (signal loop + monitor loop)
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

        self.account_id: Optional[int]   = None
        self.running:    bool             = False
        self._shutdown_evt                = asyncio.Event()
        self._last_daily_reset:  str      = ""

        # OF filter stats (for daily summary)
        self._of_signals_total: int  = 0
        self._of_signals_pass:  int  = 0

        of_status = "ENABLED" if config.ORDER_FLOW_ENABLED else "disabled"
        mode      = "🔴 LIVE" if not config.DEMO_MODE else "🟡 DEMO/PAPER"
        logger.info(
            "=" * 60
            + f"\n  UltimateTradingBot v2 starting …"
            + f"\n  Mode: {mode}"
            + f"\n  Instrument: {config.DEFAULT_INSTRUMENT}"
            + f"\n  Risk/trade: {config.RISK_PER_TRADE_PCT * 100:.2f}%"
            + f"\n  Daily loss limit: {config.DAILY_LOSS_LIMIT_PCT * 100:.2f}%"
            + f"\n  Order Flow filter: {of_status}"
            + f"\n  Prop firm: {config.PROP_FIRM}"
            + "\n" + "=" * 60
        )

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
        self.webhook.start()

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
    # SIGNAL HANDLER
    # ══════════════════════════════════════════════════════════════════════════
    async def _on_signal_received(self, payload: dict) -> None:
        command = payload.get("_command")
        if command == "flatten":
            await self._flatten_all("Operator emergency flatten")
            return
        if command == "pause":
            self.risk.is_paused = True
            await self.telegram.send_pause_alert(
                "Manual pause via webhook", self.risk.get_daily_stats()
            )
            return
        if command == "resume":
            self.risk.is_paused = False
            await self.telegram.send_custom("▶️ Bot RESUMED — accepting new signals.")
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
            return

        # ── Gate 2: Entry allowed (past OR period) ────────────────────────
        if not self.strategy.is_entry_allowed(now):
            logger.info("Gate 2 FAIL: entry not yet allowed (OR window still open)")
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

        if self.risk.is_daily_loss_limit_hit():
            self.risk.is_shutdown_today = True
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
            return

        # ── Gate 9: No existing position ─────────────────────────────────
        positions = await self.tradovate.get_positions(self.account_id)
        if positions:
            logger.info("Gate 9 FAIL: position already open")
            return

        # ── Gate 10: Strategy validation (includes Order Flow gate) ───────
        # NOTE: OrderFlowFilter is called inside parse_and_validate_signal
        #       after the primary ORB conditions pass.
        if config.ORDER_FLOW_ENABLED:
            self._of_signals_total += 1  # count all ORB attempts when OF is enabled

        signal = self.strategy.parse_and_validate_signal(payload)
        if signal is None:
            logger.info("Gate 10 FAIL: strategy / order flow validation failed")
            return

        of_result = signal.get("of_result")
        if config.ORDER_FLOW_ENABLED and of_result and of_result.passed:
            self._of_signals_pass += 1

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
        }
        self.risk.record_trade_entry(trade_info)

        await self.telegram.send_entry_alert(
            trade       = trade_info,
            equity      = equity,
            daily_stats = self.risk.get_daily_stats(),
            of_result   = of_result,           # ← v2: pass OF result
        )
        logger.info("✅ Order placed | orderId=%s", order_result.get("orderId"))

    # ══════════════════════════════════════════════════════════════════════════
    # POSITION MONITOR
    # ══════════════════════════════════════════════════════════════════════════
    async def _monitor_loop(self) -> None:
        while self.running:
            try:
                now    = datetime.now(self.tz)
                equity = await self.tradovate.get_account_equity(self.account_id)
                self.risk.update_equity(equity)

                # EOD time stop
                if self.strategy.is_force_close_time(now):
                    positions = await self.tradovate.get_positions(self.account_id)
                    if positions:
                        logger.info("⏰ Time stop: force-closing all positions")
                        await self._flatten_all("Time stop — 14:45 ET")
                        self.risk.record_trade_exit(exit_price=0, pnl=0, status="time_stop")

                # Daily loss re-check
                if not self.risk.is_shutdown_today and self.risk.is_daily_loss_limit_hit():
                    self.risk.is_shutdown_today = True
                    await self._flatten_all("Daily loss limit hit")
                    await self.telegram.send_daily_loss_limit_alert(self.risk.get_daily_stats())

                # Intraday drawdown pause
                if not self.risk.is_paused and self.risk.is_intraday_pause_triggered():
                    self.risk.is_paused = True
                    logger.warning("⚠️ Intraday DD pause triggered")
                    await self.telegram.send_pause_alert(
                        reason      = f"Intraday drawdown > {config.INTRADAY_DD_PAUSE_PCT*100:.1f}%",
                        daily_stats = self.risk.get_daily_stats(),
                    )

                # Daily reset at 09:00 ET
                today = now.date().isoformat()
                if now.hour == 9 and now.minute < 15 and today != self._last_daily_reset:
                    logger.info("📅 Performing daily reset …")
                    self.risk.reset_daily()
                    self._of_signals_total = 0
                    self._of_signals_pass  = 0
                    await self.news.fetch_calendar()
                    self._last_daily_reset = today

                # EOD daily summary at 15:05 ET
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

    # ══════════════════════════════════════════════════════════════════════════
    # RUN / SHUTDOWN
    # ══════════════════════════════════════════════════════════════════════════
    async def run(self) -> None:
        await self.initialize()
        monitor_task = asyncio.create_task(self._monitor_loop(), name="monitor")
        logger.info("🚀 Bot is live. Awaiting TradingView webhook signals …")
        try:
            await self._shutdown_evt.wait()
        except asyncio.CancelledError:
            pass
        finally:
            monitor_task.cancel()
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Shutting down …")
        self.running = False

        if self.account_id:
            positions = await self.tradovate.get_positions(self.account_id)
            if positions:
                await self._flatten_all("Bot shutdown")

        await self.telegram.send_shutdown(self.risk.get_daily_stats())
        self.webhook.stop()
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
