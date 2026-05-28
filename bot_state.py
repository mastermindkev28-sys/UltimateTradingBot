"""
bot_state.py — Shared In-Memory State Container
================================================
Single source of truth for all live data.
Imported by TradingBot (main.py), DashboardServer, and TelegramReceiver.
One singleton per process; all components read/write the same instance.

The state also manages WebSocket broadcast to connected dashboard clients
so the UI updates in real time without polling.
"""

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Set

import pytz

import config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON STATE
# ═══════════════════════════════════════════════════════════════════════════════
class BotState:
    """
    Holds all runtime state accessible to every bot component.

    Design notes:
      • Mutated only by the async trading loop — no locks needed (single-thread asyncio).
      • Dashboard server reads snapshots; never writes business-logic fields.
      • Commands from the dashboard or Telegram receiver travel back through
        a command_queue that the main loop drains each iteration.
    """

    _instance: Optional["BotState"] = None

    # ── singleton accessor ───────────────────────────────────────────────────
    @classmethod
    def get(cls) -> "BotState":
        if cls._instance is None:
            cls._instance = BotState()
        return cls._instance

    def __init__(self) -> None:
        self.tz = config.TIMEZONE

        # ── Bot lifecycle ─────────────────────────────────────────────────
        self.is_running:          bool            = False
        self.is_paused:           bool            = False
        self.is_shutdown_today:   bool            = False
        self.demo_mode:           bool            = config.DEMO_MODE
        self.instrument:          str             = config.DEFAULT_INSTRUMENT
        self.prop_firm:           str             = config.PROP_FIRM
        self.started_at:          str             = ""

        # ── Account ───────────────────────────────────────────────────────
        self.account_id:          Optional[int]   = None
        self.equity:              float           = 0.0
        self.session_start_equity: float          = 0.0
        self.high_water_equity:   float           = 0.0
        self.daily_pnl:           float           = 0.0
        self.daily_pnl_pct:       float           = 0.0
        self.intraday_dd_pct:     float           = 0.0
        self.daily_loss_limit_usd: float          = 0.0
        self.dd_buffer_pct:       float           = 100.0   # % remaining before prop-firm limit

        # ── Trading counters ──────────────────────────────────────────────
        self.daily_trades:        int             = 0
        self.winning_trades:      int             = 0
        self.losing_trades:       int             = 0
        self.consecutive_losses:  int             = 0
        self.of_signals_total:    int             = 0
        self.of_signals_pass:     int             = 0

        # ── Open trade / positions ────────────────────────────────────────
        self.open_trade:          Optional[dict]  = None
        self.open_positions:      List[dict]      = []

        # ── Trade history (rolling 100) ───────────────────────────────────
        self.trade_history:       List[dict]      = []

        # ── Equity curve (rolling 300 data points) ────────────────────────
        self._equity_curve:       List[dict]      = []   # {t, v}

        # ── Signal log (rolling 60) ───────────────────────────────────────
        self._signals:            Deque[dict]     = deque(maxlen=60)

        # ── News ──────────────────────────────────────────────────────────
        self.news_events:         List[dict]      = []
        self.news_blackout_active: bool           = False
        self.next_event_min:      Optional[int]   = None

        # ── Live log buffer (rolling 250 lines) ───────────────────────────
        self._logs:               Deque[str]      = deque(maxlen=250)

        # ── Live-editable config snapshot (updated from config module) ────
        self.live_config:         dict            = self._snapshot_config()

        # ── Command queue (dashboard / Telegram → trading loop) ───────────
        self.command_queue:       asyncio.Queue   = asyncio.Queue()

        # ── WebSocket clients registered by dashboard ─────────────────────
        self._ws_clients:         Set             = set()

        # ── Back-reference to TradingBot (set by main.py) ─────────────────
        self.bot_ref:             Any             = None

    # ── config snapshot ─────────────────────────────────────────────────────
    @staticmethod
    def _snapshot_config() -> dict:
        return {
            "instrument":              config.DEFAULT_INSTRUMENT,
            "session_start_et":        config.SESSION_START.strftime("%H:%M"),
            "session_end_et":          config.SESSION_END.strftime("%H:%M"),
            "daily_profit_target_usd": config.DAILY_PROFIT_TARGET_USD,
            "risk_per_trade_pct":      config.RISK_PER_TRADE_PCT,
            "daily_loss_limit_pct":    config.DAILY_LOSS_LIMIT_PCT,
            "daily_profit_target_pct": config.DAILY_PROFIT_TARGET_PCT,
            "max_trades_per_day":      config.MAX_TRADES_PER_DAY,
            "sl_atr_default":          config.SL_ATR_DEFAULT,
            "or_min_range_points":     config.OR_MIN_RANGE_POINTS,
            "order_flow_enabled":      config.ORDER_FLOW_ENABLED,
            "of_require_positive_delta": config.OF_REQUIRE_POSITIVE_DELTA,
            "of_require_bar_delta":    config.OF_REQUIRE_BAR_DELTA,
            "of_require_absorption":   config.OF_REQUIRE_ABSORPTION,
            "of_require_no_divergence": config.OF_REQUIRE_NO_DIVERGENCE,
            "of_allow_missing_data":   config.OF_ALLOW_MISSING_DATA,
            "news_blackout_before_min": config.NEWS_BLACKOUT_BEFORE_MIN,
            "news_blackout_after_min": config.NEWS_BLACKOUT_AFTER_MIN,
        }

    # ── state update helpers ─────────────────────────────────────────────────
    def set_running(self, running: bool) -> None:
        self.is_running = running
        if running:
            self.started_at = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S")

    def update_equity(
        self,
        equity:        float,
        session_start: float,
        high_water:    float,
    ) -> None:
        self.equity              = equity
        self.session_start_equity = session_start
        self.high_water_equity   = high_water
        self.daily_pnl           = equity - session_start
        self.daily_pnl_pct       = (self.daily_pnl / session_start * 100) if session_start else 0.0
        self.intraday_dd_pct     = ((high_water - equity) / high_water * 100) if high_water else 0.0
        self.daily_loss_limit_usd = session_start * config.DAILY_LOSS_LIMIT_PCT

        # Prop-firm buffer: how far away we are from the firm's daily limit
        max_dd_usd     = config.PROP_FIRM_PRESETS.get(config.PROP_FIRM, {}).get("max_daily_loss", 9999)
        used_loss_usd  = max(0.0, session_start - equity)
        self.dd_buffer_pct = max(0.0, (1 - used_loss_usd / max_dd_usd) * 100) if max_dd_usd else 100.0

        # Record equity curve data point
        self._equity_curve.append({
            "t": datetime.now(self.tz).strftime("%H:%M"),
            "v": round(equity, 2),
        })
        if len(self._equity_curve) > 300:
            self._equity_curve = self._equity_curve[-300:]

    def add_signal_log(
        self,
        action:      str,
        instrument:  str,
        signal_type: str,
        price:       float,
        passed:      bool,
        gate:        str = "",
        of_summary:  str = "",
        source:      str = "tradingview",
    ) -> None:
        """Record a signal attempt (pass or fail) for the dashboard signal log."""
        self._signals.append({
            "t":           datetime.now(self.tz).strftime("%H:%M:%S"),
            "action":      action,
            "instrument":  instrument,
            "signal_type": signal_type,
            "price":       price,
            "passed":      passed,
            "gate":        gate,
            "of_summary":  of_summary,
            "source":      source,
        })

    def add_log(self, level: str, msg: str) -> None:
        """Buffer a log line for the dashboard live-log panel."""
        ts = datetime.now(self.tz).strftime("%H:%M:%S")
        self._logs.append(f"{ts} [{level:<7}] {msg}")

    def record_open_trade(self, trade: dict) -> None:
        self.open_trade = trade
        self.daily_trades = max(self.daily_trades, self.daily_trades + 0)  # kept by risk_manager

    def record_trade_closed(self, trade: dict) -> None:
        self.open_trade = None
        self.trade_history.append(trade)
        if len(self.trade_history) > 100:
            self.trade_history = self.trade_history[-100:]

    def update_news(self, events: List[dict], blackout: bool, next_min: Optional[int]) -> None:
        self.news_events       = events
        self.news_blackout_active = blackout
        self.next_event_min    = next_min

    def apply_config_update(self, updates: dict) -> List[str]:
        """
        Apply a partial config update from the dashboard.
        Returns list of fields that were changed.
        Also mutates the live config.PARAM so changes take effect immediately
        without a bot restart.
        """
        changed = []
        # Mapping: dashboard key → (config attr, type coercion)
        allowed = {
            "daily_profit_target_usd": ("DAILY_PROFIT_TARGET_USD", float),
            "risk_per_trade_pct":      ("RISK_PER_TRADE_PCT",      float),
            "daily_loss_limit_pct":    ("DAILY_LOSS_LIMIT_PCT",     float),
            "daily_profit_target_pct": ("DAILY_PROFIT_TARGET_PCT",  float),
            "max_trades_per_day":      ("MAX_TRADES_PER_DAY",       int),
            "sl_atr_default":          ("SL_ATR_DEFAULT",           float),
            "or_min_range_points":     ("OR_MIN_RANGE_POINTS",      float),
            "order_flow_enabled":      ("ORDER_FLOW_ENABLED",       bool),
            "of_require_positive_delta": ("OF_REQUIRE_POSITIVE_DELTA", bool),
            "of_require_bar_delta":    ("OF_REQUIRE_BAR_DELTA",     bool),
            "of_require_absorption":   ("OF_REQUIRE_ABSORPTION",    bool),
            "of_require_no_divergence": ("OF_REQUIRE_NO_DIVERGENCE", bool),
            "news_blackout_before_min": ("NEWS_BLACKOUT_BEFORE_MIN", int),
            "news_blackout_after_min": ("NEWS_BLACKOUT_AFTER_MIN",  int),
        }
        import config as _c
        from datetime import time as _time
        for key, value in updates.items():
            try:
                if key in ("session_start_et", "session_end_et"):
                    # Parse HH:MM string and update the time object on config
                    h, m = map(int, str(value).split(":"))
                    t = _time(h, m)
                    if key == "session_start_et":
                        _c.SESSION_START = t
                    else:
                        _c.SESSION_END = t
                    self.live_config[key] = value
                    changed.append(key)
                elif key in allowed:
                    attr, coerce = allowed[key]
                    coerced = coerce(value)
                    setattr(_c, attr, coerced)
                    self.live_config[key] = coerced
                    changed.append(key)
            except (ValueError, TypeError) as exc:
                logger.warning("Config update skipped %s=%s: %s", key, value, exc)
        return changed

    # ── WebSocket management ─────────────────────────────────────────────────
    def ws_add(self, ws: Any) -> None:
        self._ws_clients.add(ws)

    def ws_remove(self, ws: Any) -> None:
        self._ws_clients.discard(ws)

    async def broadcast(self) -> None:
        """Push full state snapshot to every connected dashboard WebSocket client."""
        if not self._ws_clients:
            return
        payload = json.dumps({"type": "state", "data": self.snapshot()})
        dead: Set = set()
        for ws in list(self._ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ── snapshot ─────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Full serialisable state snapshot used by REST API and WebSocket."""
        trades_today = self.daily_trades
        win_rate = (
            round(self.winning_trades / trades_today * 100, 1) if trades_today else 0
        )
        return {
            "status": {
                "is_running":        self.is_running,
                "is_paused":         self.is_paused,
                "is_shutdown_today": self.is_shutdown_today,
                "demo_mode":         self.demo_mode,
                "instrument":        self.instrument,
                "prop_firm":         self.prop_firm,
                "started_at":        self.started_at,
                "ts":                datetime.now(self.tz).strftime("%H:%M:%S"),
            },
            "account": {
                "equity":            round(self.equity, 2),
                "daily_pnl":         round(self.daily_pnl, 2),
                "daily_pnl_pct":     round(self.daily_pnl_pct, 3),
                "intraday_dd_pct":   round(self.intraday_dd_pct, 3),
                "daily_loss_limit":  round(self.daily_loss_limit_usd, 2),
                "dd_buffer_pct":     round(self.dd_buffer_pct, 1),
                "session_start":     round(self.session_start_equity, 2),
            },
            "trading": {
                "daily_trades":      self.daily_trades,
                "max_trades":        config.MAX_TRADES_PER_DAY,
                "winning_trades":    self.winning_trades,
                "losing_trades":     self.losing_trades,
                "consecutive_losses": self.consecutive_losses,
                "win_rate":          win_rate,
                "of_enabled":        config.ORDER_FLOW_ENABLED,
                "of_pass_rate":      f"{self.of_signals_pass}/{self.of_signals_total}"
                                     if self.of_signals_total else "—",
            },
            "open_trade":  self.open_trade,
            "positions":   self.open_positions,
            "trades":      list(reversed(self.trade_history[-30:])),
            "signals":     list(self._signals),
            "equity_curve": self._equity_curve[-120:],
            "news": {
                "events":         self.news_events,
                "blackout_active": self.news_blackout_active,
                "next_event_min": self.next_event_min,
            },
            "logs":   list(self._logs)[-60:],
            "config": self.live_config,
        }
