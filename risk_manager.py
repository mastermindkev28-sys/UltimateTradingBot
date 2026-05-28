"""
risk_manager.py — Ultra-Conservative Risk Management Engine
============================================================
Every safety layer is enforced here.  Other modules call these methods;
they never make their own risk decisions.

Key responsibilities:
  • Dynamic position sizing (ATR-adjusted, equity-scaled)
  • Daily P&L and drawdown tracking
  • Hard daily-loss-limit enforcement
  • Consecutive-loss size reduction
  • Equity curve / intraday drawdown pause
  • Prop-firm consistency rule (no day > 35% of total profits)
  • Trade journal (in-memory + JSON flush)
"""

import json
import logging
import math
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Dict, List, Optional

import pytz

import config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class TradeRecord:
    trade_id:           str
    action:             str           # "BUY" | "SELL"
    instrument:         str
    contracts:          int
    entry_price:        float
    sl_price:           float
    tp1_price:          float
    tp2_price:          Optional[float]
    risk_amount:        float         # $ risked on this trade
    risk_pct:           float         # % of equity risked
    entry_time:         str           # ISO-8601
    exit_time:          Optional[str] = None
    exit_price:         Optional[float] = None
    pnl:                float         = 0.0
    status:             str           = "open"  # open | tp1 | tp2 | sl | time_stop | manual
    order_id:           Optional[str] = None
    equity_at_entry:    float         = 0.0
    equity_at_exit:     float         = 0.0


@dataclass
class DailyStats:
    date:               str           = ""
    starting_equity:    float         = 0.0
    current_equity:     float         = 0.0
    high_equity:        float         = 0.0
    realized_pnl:       float         = 0.0
    unrealized_pnl:     float         = 0.0
    trade_count:        int           = 0
    winning_trades:     int           = 0
    losing_trades:      int           = 0
    consecutive_losses: int           = 0
    is_shutdown:        bool          = False
    is_paused:          bool          = False
    trades:             List[dict]    = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
class RiskManager:
    """
    Single source of truth for all risk decisions.
    Thread-safe for use with asyncio (no threading involved, GIL-protected dict).
    """

    def __init__(self) -> None:
        self._initialized        = False
        self.starting_equity:     float           = 0.0
        self.current_equity:      float           = 0.0
        self.high_water_equity:   float           = 0.0  # session peak (for intraday DD)
        self.session_start_equity: float          = 0.0  # equity at start of today
        self.cumulative_profits:  float           = 0.0  # all-time realized profits
        self.daily_realized_pnl:  float           = 0.0
        self.daily_trades:        int             = 0
        self.consecutive_losses:  int             = 0
        self.is_shutdown_today:   bool            = False
        self.is_paused:           bool            = False
        self._open_trade:         Optional[TradeRecord] = None
        self._trade_history:      List[TradeRecord]    = []
        self._daily_stats:        DailyStats      = DailyStats()
        self._prop_cfg:           dict            = config.PROP_FIRM_PRESETS.get(
                                                        config.PROP_FIRM, {}
                                                    )

        # Ensure daily report directory exists
        os.makedirs(config.DAILY_REPORT_DIR, exist_ok=True)

    # ── initialisation ──────────────────────────────────────────────────────
    def initialize(self, equity: float) -> None:
        """Call once after authentication, before trading begins."""
        self.starting_equity      = equity
        self.current_equity       = equity
        self.high_water_equity    = equity
        self.session_start_equity = equity
        self._initialized         = True
        self._reset_daily_stats()
        logger.info(
            "RiskManager initialised | Equity=%.2f | Instrument=%s | "
            "Risk/trade=%.2f%% | Daily loss limit=%.2f%%",
            equity,
            config.DEFAULT_INSTRUMENT,
            config.RISK_PER_TRADE_PCT * 100,
            config.DAILY_LOSS_LIMIT_PCT * 100,
        )

    # ── equity tracking ─────────────────────────────────────────────────────
    def update_equity(self, equity: float) -> None:
        """Refresh current equity and update high-water mark."""
        self.current_equity = equity
        if equity > self.high_water_equity:
            self.high_water_equity = equity
        self._daily_stats.current_equity = equity

    # ── position sizing ─────────────────────────────────────────────────────
    def calculate_position_size(
        self,
        equity:            float,
        sl_points:         float,
        point_value:       float,    # e.g. 10 for MGC, 100 for GC
        consecutive_losses: int  = 0,
    ) -> Dict[str, float]:
        """
        Compute number of contracts to trade.

        Formula:
            risk_$      = equity × risk_pct
            sl_value_$  = sl_points × point_value × 1 contract
            contracts   = floor(risk_$ / sl_value_$)

        After 1 loss: cut contracts by 50%.
        Hard ceiling: instrument max AND prop-firm position limit.
        """
        if not self._initialized:
            logger.error("RiskManager.calculate_position_size called before initialize()")
            return {"contracts": 0, "risk_amount": 0.0, "risk_pct": 0.0}

        # ── 1. Determine base risk percentage ────────────────────────────
        risk_pct = config.RISK_PER_TRADE_PCT
        if consecutive_losses >= 1:
            risk_pct *= config.AFTER_1_LOSS_SIZE_REDUCTION  # 50% reduction
        risk_pct = max(config.RISK_MIN_PCT, min(config.RISK_MAX_PCT, risk_pct))

        # ── 2. Dollar amount we're willing to risk ────────────────────────
        risk_dollars = equity * risk_pct

        # ── 3. Dollar cost of 1 contract at the given SL distance ────────
        if sl_points <= 0:
            logger.error("SL points ≤ 0 — refusing to size position")
            return {"contracts": 0, "risk_amount": 0.0, "risk_pct": 0.0}

        cost_per_contract = sl_points * point_value

        # ── 4. Raw contract count ─────────────────────────────────────────
        contracts_float = risk_dollars / cost_per_contract
        contracts = max(1, math.floor(contracts_float))

        # ── 5. Apply instrument ceiling ───────────────────────────────────
        max_contracts = config.INSTRUMENTS[config.DEFAULT_INSTRUMENT].default_max_contracts
        contracts = min(contracts, max_contracts)

        # ── 6. Prop-firm safety ceiling (50% buffer) ──────────────────────
        # If the firm caps 5 contracts, we use at most 2–3
        # Here we simply cap at 40% of instrument max as extra caution
        prop_ceiling = max(1, math.floor(max_contracts * 0.40))
        contracts = min(contracts, prop_ceiling)

        # ── 7. Actual risk at final size ──────────────────────────────────
        actual_risk = contracts * cost_per_contract

        logger.info(
            "Position sizing | equity=%.2f risk_pct=%.3f%% sl_pts=%.2f "
            "pt_val=%.0f → %d contracts (risk=$%.2f / %.3f%%)",
            equity, risk_pct * 100, sl_points, point_value,
            contracts, actual_risk, actual_risk / equity * 100,
        )

        return {
            "contracts":   float(contracts),
            "risk_amount": actual_risk,
            "risk_pct":    actual_risk / equity * 100,
        }

    # ── daily limit checks ──────────────────────────────────────────────────
    def is_daily_loss_limit_hit(self) -> bool:
        """Return True if today's loss has reached or exceeded the hard limit."""
        loss = self.session_start_equity - self.current_equity
        limit = self.session_start_equity * config.DAILY_LOSS_LIMIT_PCT

        # Also check realized P&L (in case of open profitable positions masking a realized loss)
        realized_loss = -self.daily_realized_pnl if self.daily_realized_pnl < 0 else 0
        hit = (loss >= limit) or (realized_loss >= limit)

        if hit:
            logger.critical(
                "🛑 DAILY LOSS LIMIT HIT | loss=%.2f limit=%.2f",
                max(loss, realized_loss), limit,
            )
        return hit

    def is_daily_profit_target_hit(self) -> bool:
        """Return True if we've reached the daily profit goal (stop trading early).

        If DAILY_PROFIT_TARGET_USD > 0 that fixed dollar amount takes precedence;
        otherwise the percentage-based target is used.
        """
        gain = self.current_equity - self.session_start_equity
        if config.DAILY_PROFIT_TARGET_USD > 0:
            return gain >= config.DAILY_PROFIT_TARGET_USD
        target = self.session_start_equity * config.DAILY_PROFIT_TARGET_PCT
        return gain >= target

    def is_intraday_pause_triggered(self) -> bool:
        """
        Return True if intraday drawdown from the session high exceeds the
        pause threshold (default 0.5%).
        """
        if self.high_water_equity <= 0:
            return False
        dd_pct = (self.high_water_equity - self.current_equity) / self.high_water_equity
        return dd_pct >= config.INTRADAY_DD_PAUSE_PCT

    def would_violate_consistency_rule(self, potential_gain: float) -> bool:
        """
        Prop-firm consistency check: would this trade's gain push today's
        contribution above 35% of total cumulative profits?
        """
        if self.cumulative_profits <= 0:
            return False  # no profits yet — rule not relevant
        projected_day_profit = self.daily_realized_pnl + potential_gain
        share = projected_day_profit / self.cumulative_profits
        if share > config.MAX_DAY_PROFIT_SHARE:
            logger.warning(
                "Consistency rule: today's projected contribution %.1f%% > %.0f%% limit",
                share * 100, config.MAX_DAY_PROFIT_SHARE * 100,
            )
            return True
        return False

    # ── trade lifecycle ─────────────────────────────────────────────────────
    def record_trade_entry(self, trade_info: dict) -> TradeRecord:
        """Create a TradeRecord for a new entry and update daily counters."""
        import uuid
        trade = TradeRecord(
            trade_id        = str(uuid.uuid4())[:8],
            action          = trade_info["action"],
            instrument      = trade_info["instrument"],
            contracts       = int(trade_info["contracts"]),
            entry_price     = float(trade_info["entry_price"]),
            sl_price        = float(trade_info["sl_price"]),
            tp1_price       = float(trade_info["tp1_price"]),
            tp2_price       = trade_info.get("tp2_price"),
            risk_amount     = float(trade_info["risk_amount"]),
            risk_pct        = float(trade_info["risk_pct"]),
            entry_time      = trade_info.get("entry_time", datetime.now().isoformat()),
            order_id        = trade_info.get("order_id"),
            equity_at_entry = self.current_equity,
        )
        self._open_trade = trade
        self.daily_trades += 1
        self._daily_stats.trade_count += 1
        self._daily_stats.trades.append(asdict(trade))
        logger.info("📝 Trade recorded: %s %d×%s @ %.2f",
                    trade.action, trade.contracts, trade.instrument, trade.entry_price)
        return trade

    def record_trade_exit(
        self,
        exit_price: float,
        pnl:        float,
        status:     str = "closed",
    ) -> Optional[TradeRecord]:
        """Close the active trade record and update cumulative stats."""
        if not self._open_trade:
            logger.warning("record_trade_exit called with no open trade")
            return None

        trade = self._open_trade
        trade.exit_price    = exit_price
        trade.exit_time     = datetime.now().isoformat()
        trade.pnl           = pnl
        trade.status        = status
        trade.equity_at_exit = self.current_equity

        self.daily_realized_pnl += pnl
        self._daily_stats.realized_pnl = self.daily_realized_pnl

        if pnl > 0:
            self.winning_trades += 1
            self._daily_stats.winning_trades += 1
            self.consecutive_losses = 0
            self.cumulative_profits += pnl
        else:
            self.losing_trades += 1
            self._daily_stats.losing_trades += 1
            self.consecutive_losses += 1
            self._daily_stats.consecutive_losses = self.consecutive_losses

        self._trade_history.append(trade)
        self._open_trade = None

        logger.info(
            "📝 Trade closed: %s @ %.2f | P&L=%.2f | cons_losses=%d",
            status, exit_price, pnl, self.consecutive_losses,
        )
        self._flush_daily_stats()
        return trade

    # ── daily reset ─────────────────────────────────────────────────────────
    def reset_daily(self) -> None:
        """Call at the start of each trading session (e.g. 9:00 AM ET)."""
        self.session_start_equity = self.current_equity
        self.high_water_equity    = self.current_equity
        self.daily_realized_pnl   = 0.0
        self.daily_trades         = 0
        self.consecutive_losses   = 0
        self.is_shutdown_today    = False
        self.is_paused            = False
        self.winning_trades       = 0
        self.losing_trades        = 0
        self._reset_daily_stats()
        logger.info("📅 Daily stats reset | session_start_equity=%.2f", self.session_start_equity)

    def _reset_daily_stats(self) -> None:
        today = date.today().isoformat()
        self._daily_stats = DailyStats(
            date             = today,
            starting_equity  = self.current_equity,
            current_equity   = self.current_equity,
            high_equity      = self.current_equity,
        )

    # ── reporting ───────────────────────────────────────────────────────────
    def get_daily_stats(self) -> dict:
        """Return a snapshot of today's trading stats for reporting."""
        stats = asdict(self._daily_stats)
        stats.update({
            "current_equity":     self.current_equity,
            "session_start_eq":   self.session_start_equity,
            "daily_pnl":          self.daily_realized_pnl,
            "daily_pnl_pct":      (self.daily_realized_pnl / self.session_start_equity * 100
                                   if self.session_start_equity else 0),
            "daily_loss_limit_$": self.session_start_equity * config.DAILY_LOSS_LIMIT_PCT,
            "remaining_trades":   max(0, config.MAX_TRADES_PER_DAY - self.daily_trades),
            "consecutive_losses": self.consecutive_losses,
            "is_shutdown":        self.is_shutdown_today,
            "is_paused":          self.is_paused,
            "intraday_dd_pct":   (
                (self.high_water_equity - self.current_equity) / self.high_water_equity * 100
                if self.high_water_equity else 0
            ),
            "prop_firm":          config.PROP_FIRM,
        })
        return stats

    def _flush_daily_stats(self) -> None:
        """Persist daily stats to a JSON file after each trade close."""
        try:
            today = date.today().isoformat()
            path = os.path.join(config.DAILY_REPORT_DIR, f"{today}.json")
            with open(path, "w") as f:
                json.dump(self.get_daily_stats(), f, indent=2, default=str)
        except Exception as exc:
            logger.error("Failed to flush daily stats: %s", exc)

    # ── convenience props ───────────────────────────────────────────────────
    @property
    def winning_trades(self) -> int:
        return self._daily_stats.winning_trades

    @winning_trades.setter
    def winning_trades(self, v: int) -> None:
        self._daily_stats.winning_trades = v

    @property
    def losing_trades(self) -> int:
        return self._daily_stats.losing_trades

    @losing_trades.setter
    def losing_trades(self, v: int) -> None:
        self._daily_stats.losing_trades = v
