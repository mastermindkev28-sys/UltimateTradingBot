"""
telegram_receiver.py — Telegram Signal & Command Receiver
==========================================================
Polls your @Quant_PF_signals_bot for incoming messages and routes them
to the trading bot as signals or operator commands.

Two classes of messages are accepted:
────────────────────────────────────
1. TRADE SIGNALS  — execute a trade through the full validation pipeline
   Text format (simple):
     BUY MGC
     SELL GC
     BUY MGC 1950.5          ← includes price hint

   JSON format (full — same structure as TradingView webhook):
     {"action":"BUY","instrument":"MGC","signal_type":"ORB","price":1950.5,...}

2. OPERATOR COMMANDS  — control the bot
   /status         → reply with current bot status
   /pause          → pause new entries
   /resume         → resume entries
   /flatten        → emergency close all positions
   /stop           → graceful bot shutdown
   /config         → show live config
   /risk <pct>     → set risk_per_trade_pct (e.g. /risk 0.003)
   /news           → list today's news events
   /trades         → show today's trade summary

Security
────────
Only messages from TELEGRAM_SIGNAL_CHAT_ID are accepted.
All other senders are silently ignored.

Polling
───────
Uses long-poll (timeout=25 s) so latency is near-instant without
hammering the Telegram API.  Runs as an asyncio task alongside the bot.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

import aiohttp

import config
from bot_state import BotState, get_state

logger = logging.getLogger(__name__)

# Chat ID allowed to send signals/commands to this bot
SIGNAL_CHAT_ID = os.getenv("TELEGRAM_SIGNAL_CHAT_ID", "")

# If the signal chat and alert chat are the same, use the main chat ID
if not SIGNAL_CHAT_ID:
    SIGNAL_CHAT_ID = config.TELEGRAM_CHAT_ID

TG_BASE = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT = 25     # seconds for long poll
RETRY_SLEEP  = 5      # seconds between error retries


class TelegramReceiver:
    """
    Polls the Telegram Bot API for new messages and routes them to
    the trading bot's signal/command pipeline.
    """

    def __init__(self, on_signal: Callable[[dict], Coroutine]) -> None:
        """
        on_signal: async callable that accepts a webhook-compatible payload dict.
                   This is the same callback used by WebhookServer so all
                   validation gates still apply.
        """
        self._token      = config.TELEGRAM_BOT_TOKEN
        self._chat_id    = SIGNAL_CHAT_ID
        self._on_signal  = on_signal
        self._offset     = 0          # Telegram update_id offset
        self._running    = False
        self._state      = BotState.get()
        self._session: Optional[aiohttp.ClientSession] = None

        self._enabled    = bool(self._token)
        if not self._enabled:
            logger.warning("TelegramReceiver: TELEGRAM_BOT_TOKEN not set — receiver disabled")
        elif not self._chat_id:
            logger.warning("TelegramReceiver: TELEGRAM_SIGNAL_CHAT_ID not set — receiver disabled")
            self._enabled = False
        else:
            logger.info(
                "TelegramReceiver ready | authorised chat: %s | "
                "polling every %ds", self._chat_id, POLL_TIMEOUT
            )

    # ── session ──────────────────────────────────────────────────────────────
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 10)
            )
        return self._session

    # ── Telegram API helpers ─────────────────────────────────────────────────
    async def _api(self, method: str, params: Optional[dict] = None) -> Optional[dict]:
        url = f"{TG_BASE.format(token=self._token)}/{method}"
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.error("Telegram API error (%s): %s", method, data)
                    return None
                return data
        except asyncio.TimeoutError:
            return None
        except Exception as exc:
            logger.error("Telegram API exception (%s): %s", method, exc)
            return None

    async def _reply(self, chat_id: str, text: str) -> None:
        """Send a plain-text reply back to the operator."""
        await self._api("sendMessage", {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
        })

    # ── main polling loop ────────────────────────────────────────────────────
    async def run(self) -> None:
        """Start the long-polling loop.  Call as an asyncio task."""
        if not self._enabled:
            return

        self._running = True
        logger.info("TelegramReceiver polling started")

        while self._running:
            try:
                data = await self._api("getUpdates", {
                    "offset":  self._offset,
                    "timeout": POLL_TIMEOUT,
                })
                if data and data.get("result"):
                    for update in data["result"]:
                        self._offset = update["update_id"] + 1
                        await self._handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("TelegramReceiver error: %s", exc)
                await asyncio.sleep(RETRY_SLEEP)

        logger.info("TelegramReceiver polling stopped")

    def stop(self) -> None:
        self._running = False

    # ── update dispatch ──────────────────────────────────────────────────────
    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        text    = (msg.get("text") or "").strip()

        if not text:
            return

        # ── Security: only accept from authorised chat ────────────────────
        if chat_id != str(self._chat_id):
            logger.info(
                "TelegramReceiver: ignoring message from unauthorised chat %s", chat_id
            )
            return

        logger.info("📱 Telegram message from %s: %s", chat_id, text[:100])
        self._state.add_log("INFO", f"Telegram: {text[:80]}")

        # ── Route to command or signal ────────────────────────────────────
        if text.startswith("/"):
            await self._handle_command(text, chat_id)
        else:
            await self._handle_signal_text(text, chat_id)

    # ── command handler ──────────────────────────────────────────────────────
    async def _handle_command(self, text: str, chat_id: str) -> None:
        parts = text.split()
        cmd   = parts[0].lower()

        if cmd == "/status":
            await self._cmd_status(chat_id)

        elif cmd == "/pause":
            self._state.is_paused = True
            if self._state.bot_ref:
                self._state.bot_ref.risk.is_paused = True
            await self._reply(chat_id, "⏸ Bot <b>PAUSED</b> — no new entries until /resume")
            logger.warning("Bot paused via Telegram command")

        elif cmd == "/resume":
            self._state.is_paused = False
            if self._state.bot_ref:
                self._state.bot_ref.risk.is_paused = False
            await self._reply(chat_id, "▶️ Bot <b>RESUMED</b> — accepting signals again")
            logger.info("Bot resumed via Telegram command")

        elif cmd == "/flatten":
            await self._reply(chat_id, "🚨 Flattening all positions…")
            await self._on_signal({
                "_command": "flatten",
                "secret":   config.WEBHOOK_SECRET,
            })

        elif cmd == "/stop":
            await self._reply(chat_id, "🔴 Stopping bot…")
            if self._state.bot_ref:
                self._state.bot_ref.stop()

        elif cmd == "/trades":
            await self._cmd_trades(chat_id)

        elif cmd == "/config":
            await self._cmd_config(chat_id)

        elif cmd == "/news":
            await self._cmd_news(chat_id)

        elif cmd == "/risk" and len(parts) == 2:
            try:
                pct = float(parts[1])
                if 0.001 <= pct <= 0.004:
                    import config as _c
                    _c.RISK_PER_TRADE_PCT = pct
                    self._state.live_config["risk_per_trade_pct"] = pct
                    await self._reply(chat_id, f"✅ Risk/trade set to <b>{pct*100:.3f}%</b>")
                else:
                    await self._reply(chat_id, "⚠️ Risk must be 0.001–0.004 (0.1%–0.4%)")
            except ValueError:
                await self._reply(chat_id, "Usage: /risk 0.003")

        elif cmd == "/help":
            await self._reply(chat_id, self._help_text())

        else:
            await self._reply(chat_id, f"Unknown command: {cmd}\n\n{self._help_text()}")

    # ── signal text parser ───────────────────────────────────────────────────
    async def _handle_signal_text(self, text: str, chat_id: str) -> None:
        """
        Parse a plain-text or JSON signal from Telegram and forward it
        to the bot's signal pipeline (same as a TradingView webhook).

        Supported formats:
          BUY MGC
          SELL GC
          BUY MGC 1950.5
          {"action":"BUY","instrument":"MGC","signal_type":"ORB",...}
        """
        # ── Try JSON first ────────────────────────────────────────────────
        if text.startswith("{"):
            try:
                payload = json.loads(text)
                payload["secret"] = config.WEBHOOK_SECRET  # inject secret
                payload["_source"] = "telegram"
                await self._forward_signal(payload, chat_id)
                return
            except json.JSONDecodeError:
                pass

        # ── Simple text parsing  e.g. "BUY MGC" or "SELL GC 1950.5" ─────
        parts  = text.upper().split()
        if len(parts) >= 2 and parts[0] in ("BUY", "SELL"):
            action     = parts[0]
            instrument = parts[1] if parts[1] in config.INSTRUMENTS else config.DEFAULT_INSTRUMENT
            price_hint = float(parts[2]) if len(parts) >= 3 else 0.0

            payload = {
                "action":      action,
                "instrument":  instrument,
                "signal_type": "MANUAL",      # bypasses ORB filter in strategy_logic
                "price":       price_hint,
                "atr":         2.0,           # default ATR — bot will use this for SL sizing
                "rsi":         50.0,
                "adx":         20.0,
                "vwap":        price_hint,
                "ema20":       price_hint,
                "or_high":     price_hint + 2.0,
                "or_low":      price_hint - 2.0,
                "volume":      9999,
                "volume_avg":  1000,
                "secret":      config.WEBHOOK_SECRET,
                "_source":     "telegram",
            }
            await self._forward_signal(payload, chat_id)
            return

        # ── Signals from @Quant_PF_signals_bot ─────────────────────────────
        # Flexible keyword parser — many signal bots use free-form text like:
        # "🟢 BUY GOLD @ 1950.5 SL 1947 TP 1958"
        text_lower = text.lower()
        action = None
        if any(k in text_lower for k in ("buy", "long", "🟢", "↑", "▲")):
            action = "BUY"
        elif any(k in text_lower for k in ("sell", "short", "🔴", "↓", "▼")):
            action = "SELL"

        if action:
            # Try to extract a price
            import re
            prices = re.findall(r"\d{4,5}(?:\.\d{1,2})?", text)
            price_hint = float(prices[0]) if prices else 0.0

            instrument = "MGC" if "micro" in text_lower else config.DEFAULT_INSTRUMENT
            if "gc" in text_lower or "gold" in text_lower:
                instrument = config.DEFAULT_INSTRUMENT

            payload = {
                "action":      action,
                "instrument":  instrument,
                "signal_type": "MANUAL",
                "price":       price_hint,
                "atr":         2.0,
                "rsi":         50.0,
                "adx":         20.0,
                "vwap":        price_hint,
                "ema20":       price_hint,
                "or_high":     price_hint + 2.0,
                "or_low":      price_hint - 2.0,
                "volume":      9999,
                "volume_avg":  1000,
                "secret":      config.WEBHOOK_SECRET,
                "_source":     "telegram_quant_pf",
            }
            await self._reply(chat_id,
                f"📡 Parsed signal: <b>{action} {instrument}</b> @ {price_hint}\n"
                f"Routing through validation pipeline…")
            await self._forward_signal(payload, chat_id)
        else:
            await self._reply(chat_id,
                "❓ Could not parse as a trade signal.\n"
                "Send: <code>BUY MGC</code> or <code>SELL GC 1950.5</code>")

    async def _forward_signal(self, payload: dict, chat_id: str) -> None:
        """Forward parsed payload to the main signal pipeline."""
        try:
            await self._on_signal(payload)
        except Exception as exc:
            await self._reply(chat_id, f"❌ Signal error: {exc}")

    # ── command reply builders ───────────────────────────────────────────────
    async def _cmd_status(self, chat_id: str) -> None:
        s = self._state
        status = "🟢 RUNNING" if s.is_running else "⚫ STOPPED"
        if s.is_paused:       status = "⏸ PAUSED"
        if s.is_shutdown_today: status = "🛑 SHUTDOWN (daily limit)"

        pnl_icon = "📈" if s.daily_pnl >= 0 else "📉"
        mode = "🟡 DEMO" if s.demo_mode else "🔴 LIVE"

        text = (
            f"<b>📊 Bot Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Status:     {status}\n"
            f"Mode:       {mode}\n"
            f"Instrument: {s.instrument}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Equity:     ${s.equity:,.2f}\n"
            f"{pnl_icon} Day P&L:  ${s.daily_pnl:+.2f} ({s.daily_pnl_pct:+.3f}%)\n"
            f"DD Buffer:  {s.dd_buffer_pct:.1f}%\n"
            f"Intraday DD:{s.intraday_dd_pct:.3f}%\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Trades:     {s.daily_trades}/{config.MAX_TRADES_PER_DAY}\n"
            f"W/L:        {s.winning_trades}W / {s.losing_trades}L\n"
            f"Cons.Losses:{s.consecutive_losses}\n"
        )
        await self._reply(chat_id, text)

    async def _cmd_trades(self, chat_id: str) -> None:
        history = list(reversed(self._state.trade_history[-5:]))
        if not history:
            await self._reply(chat_id, "No trades today.")
            return
        lines = ["<b>📋 Recent Trades</b>"]
        for t in history:
            icon = "✅" if t.get("pnl", 0) > 0 else "❌"
            lines.append(
                f"{icon} {t.get('action','')} {t.get('instrument','')} "
                f"@ {t.get('entry_price', 0):.2f} | "
                f"P&L: ${t.get('pnl', 0):+.2f}"
            )
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_config(self, chat_id: str) -> None:
        c = self._state.live_config
        text = (
            f"<b>⚙️ Live Config</b>\n"
            f"Risk/trade:  {c.get('risk_per_trade_pct',0)*100:.3f}%\n"
            f"DL Limit:    {c.get('daily_loss_limit_pct',0)*100:.3f}%\n"
            f"Max trades:  {c.get('max_trades_per_day',3)}\n"
            f"SL ATR mult: {c.get('sl_atr_default',1.0)}\n"
            f"OR min range:{c.get('or_min_range_points',1.5)} pts\n"
            f"OF enabled:  {c.get('order_flow_enabled', False)}\n"
        )
        await self._reply(chat_id, text)

    async def _cmd_news(self, chat_id: str) -> None:
        events = self._state.news_events
        if not events:
            await self._reply(chat_id, "📰 No high-impact events today.")
            return
        lines = ["<b>📰 Today's News Events</b>"]
        for e in events:
            lines.append(f"• {e.get('time','')} — {e.get('title','')}")
        if self._state.news_blackout_active:
            lines.append("\n🚫 <b>BLACKOUT ACTIVE NOW</b>")
        await self._reply(chat_id, "\n".join(lines))

    @staticmethod
    def _help_text() -> str:
        return (
            "<b>📡 Available Commands</b>\n"
            "/status   — Bot status & account stats\n"
            "/pause    — Pause new entries\n"
            "/resume   — Resume entries\n"
            "/flatten  — Emergency close all positions\n"
            "/stop     — Graceful shutdown\n"
            "/trades   — Recent trade list\n"
            "/config   — Show live configuration\n"
            "/news     — Today's news events\n"
            "/risk N   — Set risk % (e.g. /risk 0.003)\n"
            "\n<b>📊 Send a Signal</b>\n"
            "<code>BUY MGC</code>        — long Micro Gold\n"
            "<code>SELL GC 1950.5</code> — short Gold @ 1950.5\n"
            "Or send full JSON payload (TradingView format)"
        )

    async def close(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
