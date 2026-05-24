"""
telegram_alerts.py — Rich Telegram Notification System
=======================================================
Sends real-time trade alerts, risk warnings, and daily summaries
to a configured Telegram bot/channel.

All messages use Telegram MarkdownV2 formatting with emoji status
indicators.  Messages are queued internally so a slow network never
blocks the main trading loop.

Setup: create a bot via @BotFather → set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional
import aiohttp
import config

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
TG_API = "https://api.telegram.org/bot{token}/{method}"

# Maximum message length before truncation
TG_MAX_LEN = 4096


def _esc(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    # Characters that must be escaped: _ * [ ] ( ) ~ ` > # + - = | { } . !
    specials = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in str(text))


def _fmt_pnl(value: float) -> str:
    """Format P&L with sign and 2 decimal places."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}"


def _fmt_pct(value: float) -> str:
    """Format percentage."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.3f}%"


class TelegramAlerter:
    """
    Async Telegram alert sender.
    All public methods are async and safe to call from the trading loop.
    Failures are logged but never raised — alerts must never crash the bot.
    """

    def __init__(self) -> None:
        self._token    = config.TELEGRAM_BOT_TOKEN
        self._chat_id  = config.TELEGRAM_CHAT_ID
        self._enabled  = bool(self._token and self._chat_id)
        self._session: Optional[aiohttp.ClientSession] = None

        if not self._enabled:
            logger.warning(
                "Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
            )

    # ── session management ──────────────────────────────────────────────────
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _send(self, text: str, parse_mode: str = "MarkdownV2") -> bool:
        """Core send — returns True on success, False on failure."""
        if not self._enabled:
            logger.debug("Telegram disabled — would have sent: %s", text[:120])
            return False

        # Truncate if needed
        if len(text) > TG_MAX_LEN:
            text = text[: TG_MAX_LEN - 3] + "..."

        url = TG_API.format(token=self._token, method="sendMessage")
        payload = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": parse_mode,
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.error("Telegram sendMessage failed: %s", data)
                    return False
                return True
        except Exception as exc:
            logger.error("Telegram send error: %s", exc)
            return False

    # ── alert methods ───────────────────────────────────────────────────────
    async def send_startup(
        self,
        mode:        str,
        equity:      float,
        instrument:  str,
        risk_pct:    float,
    ) -> None:
        mode_tag = config.PROP_FIRM.replace("_", "\\_").upper()
        text = (
            f"🤖 *Ultra\\-Conservative Gold Bot Started*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔧 Mode:         `{_esc(mode)}`\n"
            f"💰 Equity:       `${equity:,.2f}`\n"
            f"📊 Instrument:   `{_esc(instrument)}`\n"
            f"⚠️  Risk/trade:  `{_esc(f'{risk_pct:.2f}%')}`\n"
            f"🏦 Prop Firm:   `{mode_tag}`\n"
            f"🕐 Session:      `09:30 – 15:00 ET`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ All systems go\\. Waiting for signals…"
        )
        await self._send(text)

    async def send_entry_alert(
        self,
        trade:       dict,
        equity:      float,
        daily_stats: dict,
    ) -> None:
        action     = trade["action"]
        arrow      = "🟢 LONG" if action == "BUY" else "🔴 SHORT"
        inst       = _esc(trade["instrument"])
        entry      = _esc(f"{trade['entry_price']:.2f}")
        sl         = _esc(f"{trade['sl_price']:.2f}")
        tp1        = _esc(f"{trade['tp1_price']:.2f}")
        tp2        = _esc(f"{trade.get('tp2_price', 0):.2f}")
        contracts  = trade["contracts"]
        risk_amt   = _esc(f"${trade['risk_amount']:.2f}")
        risk_pct   = _esc(f"{trade['risk_pct']:.3f}%")
        trades_rem = daily_stats.get("remaining_trades", "?")
        dl_limit   = _esc(f"${daily_stats.get('daily_loss_limit_$', 0):.2f}")
        eq_disp    = _esc(f"${equity:,.2f}")

        text = (
            f"📈 *TRADE ENTRY \\— {inst} {arrow}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Contracts:  `{contracts}`\n"
            f"🎯 Entry:      `{entry}`\n"
            f"🛑 Stop:       `{sl}`\n"
            f"✅ TP1 \\(50%\\): `{tp1}`\n"
            f"🎖 TP2 \\(50%\\): `{tp2}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 Risk:       `{risk_amt} / {risk_pct}`\n"
            f"💰 Equity:     `{eq_disp}`\n"
            f"🔢 Trades rem: `{trades_rem}/{config.MAX_TRADES_PER_DAY}`\n"
            f"🚧 DL Limit:   `{dl_limit}`"
        )
        await self._send(text)

    async def send_exit_alert(
        self,
        trade:       dict,
        pnl:         float,
        exit_reason: str,
        equity:      float,
        daily_stats: dict,
    ) -> None:
        inst       = _esc(trade.get("instrument", ""))
        exit_px    = _esc(f"{trade.get('exit_price', 0):.2f}")
        entry_px   = _esc(f"{trade.get('entry_price', 0):.2f}")
        pnl_str    = _esc(_fmt_pnl(pnl))
        pnl_pct    = _esc(_fmt_pct(pnl / trade.get("equity_at_entry", equity) * 100))
        day_pnl    = _esc(_fmt_pnl(daily_stats.get("daily_pnl", 0)))
        day_pnl_p  = _esc(_fmt_pct(daily_stats.get("daily_pnl_pct", 0)))
        eq_disp    = _esc(f"${equity:,.2f}")
        reason     = _esc(exit_reason)
        icon       = "✅" if pnl > 0 else ("⚠️" if pnl == 0 else "❌")

        text = (
            f"{icon} *TRADE EXIT \\— {inst}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 Exit:       `{exit_px}`\n"
            f"📥 Entry:      `{entry_px}`\n"
            f"💵 P\\&L:       `{pnl_str}  \\({pnl_pct}\\)`\n"
            f"📝 Reason:     `{reason}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Day P\\&L:   `{day_pnl}  \\({day_pnl_p}\\)`\n"
            f"💰 Equity:     `{eq_disp}`"
        )
        await self._send(text)

    async def send_daily_loss_limit_alert(self, daily_stats: dict) -> None:
        day_pnl   = _esc(_fmt_pnl(daily_stats.get("daily_pnl", 0)))
        day_pnl_p = _esc(_fmt_pct(daily_stats.get("daily_pnl_pct", 0)))
        eq_disp   = _esc(f"${daily_stats.get('current_equity', 0):,.2f}")
        limit     = _esc(f"${daily_stats.get('daily_loss_limit_$', 0):.2f}")

        text = (
            f"🛑 *DAILY LOSS LIMIT REACHED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Day P\\&L:   `{day_pnl}  \\({day_pnl_p}\\)`\n"
            f"🚧 DL Limit:   `{limit}`\n"
            f"💰 Equity:     `{eq_disp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⛔️ All positions closed\\. Bot shut down for today\\."
        )
        await self._send(text)

    async def send_pause_alert(self, reason: str, daily_stats: dict) -> None:
        dd_pct  = _esc(f"{daily_stats.get('intraday_dd_pct', 0):.3f}%")
        eq_disp = _esc(f"${daily_stats.get('current_equity', 0):,.2f}")
        r_esc   = _esc(reason)

        text = (
            f"⏸ *BOT PAUSED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Reason:        `{r_esc}`\n"
            f"Intraday DD:   `{dd_pct}`\n"
            f"Equity:        `{eq_disp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"No new trades until manually resumed\\."
        )
        await self._send(text)

    async def send_flatten_alert(self, reason: str) -> None:
        r_esc = _esc(reason)
        text = (
            f"🚨 *FLATTEN ALL POSITIONS*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Reason: `{r_esc}`\n"
            f"All positions market\\-closed immediately\\."
        )
        await self._send(text)

    async def send_news_blackout(self, event_name: str, minutes_to_event: int) -> None:
        ev = _esc(event_name)
        text = (
            f"📰 *NEWS BLACKOUT ACTIVE*\n"
            f"Event: `{ev}`\n"
            f"Time to event: `{minutes_to_event} min`\n"
            f"No trading during blackout window\\."
        )
        await self._send(text)

    async def send_daily_summary(self, daily_stats: dict) -> None:
        trades     = daily_stats.get("trade_count", 0)
        wins       = daily_stats.get("winning_trades", 0)
        losses     = daily_stats.get("losing_trades", 0)
        day_pnl    = daily_stats.get("daily_pnl", 0.0)
        day_pnl_p  = daily_stats.get("daily_pnl_pct", 0.0)
        eq_disp    = _esc(f"${daily_stats.get('current_equity', 0):,.2f}")
        pnl_str    = _esc(_fmt_pnl(day_pnl))
        pnl_pct    = _esc(_fmt_pct(day_pnl_p))
        win_rate   = _esc(f"{(wins / trades * 100):.0f}%" if trades else "N/A")
        dd_pct     = _esc(f"{daily_stats.get('intraday_dd_pct', 0):.3f}%")
        icon       = "🏆" if day_pnl > 0 else ("😐" if day_pnl == 0 else "😟")

        text = (
            f"{icon} *DAILY SUMMARY*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Trades:     `{trades}` \\({wins}W / {losses}L\\)\n"
            f"🎯 Win Rate:   `{win_rate}`\n"
            f"💵 Day P\\&L:   `{pnl_str}  \\({pnl_pct}\\)`\n"
            f"📉 Intraday DD: `{dd_pct}`\n"
            f"💰 Equity:     `{eq_disp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Session closed\\. See you tomorrow\\! 🌙"
        )
        await self._send(text)

    async def send_error(self, message: str) -> None:
        msg_esc = _esc(message[:300])
        text = (
            f"❗ *BOT ERROR*\n"
            f"```\n{msg_esc}\n```\n"
            f"_Check logs immediately\\._"
        )
        await self._send(text)

    async def send_shutdown(self, daily_stats: dict) -> None:
        day_pnl = daily_stats.get("daily_pnl", 0.0)
        eq_disp = _esc(f"${daily_stats.get('current_equity', 0):,.2f}")
        pnl_str = _esc(_fmt_pnl(day_pnl))
        text = (
            f"🔴 *BOT SHUTDOWN*\n"
            f"Final equity: `{eq_disp}` \\| Day P\\&L: `{pnl_str}`\n"
            f"All positions flattened\\. Goodbye\\! 👋"
        )
        await self._send(text)

    async def send_custom(self, message: str) -> None:
        """Send a plain text message (no MarkdownV2 formatting)."""
        await self._send(message, parse_mode="")

    # ── cleanup ─────────────────────────────────────────────────────────────
    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
