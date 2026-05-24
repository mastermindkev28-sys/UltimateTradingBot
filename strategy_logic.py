"""
strategy_logic.py — Strategy Signal Validation Engine
======================================================
The Python bot receives pre-computed signals from the TradingView Pine Script
via webhook.  This module acts as a second validation layer — it independently
re-checks every condition before allowing the trade to proceed.

Responsibilities:
  • Parse and validate incoming TradingView webhook payloads
  • Enforce opening-range timing rules
  • Validate RSI / ATR / volume / ADX filters
  • Track intrabar opening-range state
  • Determine session status
  • Compute SL / TP prices from ATR

Independent indicator calculation is included so the bot can run
without a live TradingView connection (e.g. for unit testing or
if TradingView signals are delayed).
"""

import logging
import math
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

import pytz

import config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# INDICATOR UTILITIES  (pure functions — no side effects)
# ═══════════════════════════════════════════════════════════════════════════════

def ema(values: List[float], period: int) -> List[float]:
    """Exponential moving average — returns same-length list."""
    if not values or period < 1:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(closes: List[float], period: int = 14) -> float:
    """Wilder RSI.  Returns the last value."""
    if len(closes) < period + 1:
        return 50.0   # neutral default
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1 + rs)


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Wilder ATR.  Returns the last value."""
    if len(closes) < 2 or len(highs) < 2:
        return 1.0
    trs = [true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, len(closes))]
    if len(trs) < period:
        return sum(trs) / len(trs)
    # Wilder smoothing
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def adx(
    highs: List[float],
    lows:  List[float],
    closes: List[float],
    period: int = 14,
) -> float:
    """Simple ADX calculation (Wilder).  Returns last ADX value."""
    if len(closes) < period * 2:
        return 25.0   # neutral default

    dm_plus, dm_minus, trs = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i]  - highs[i - 1]
        down = lows[i - 1] - lows[i]
        dm_plus.append(up   if up > down and up > 0   else 0)
        dm_minus.append(down if down > up and down > 0 else 0)
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))

    def smooth(arr: List[float], n: int) -> List[float]:
        out = [sum(arr[:n])]
        for v in arr[n:]:
            out.append(out[-1] - out[-1] / n + v)
        return out

    s_tr  = smooth(trs, period)
    s_dmp = smooth(dm_plus, period)
    s_dmm = smooth(dm_minus, period)

    di_plus  = [100 * p / t if t else 0 for p, t in zip(s_dmp, s_tr)]
    di_minus = [100 * m / t if t else 0 for m, t in zip(s_dmm, s_tr)]
    dx_list  = [
        100 * abs(p - m) / (p + m) if (p + m) > 0 else 0
        for p, m in zip(di_plus, di_minus)
    ]

    if len(dx_list) < period:
        return sum(dx_list) / len(dx_list)

    adx_val = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val


def session_vwap(
    highs:   List[float],
    lows:    List[float],
    closes:  List[float],
    volumes: List[float],
) -> float:
    """Intraday VWAP from the first bar of the list."""
    total_pv = sum(
        ((h + l + c) / 3) * v
        for h, l, c, v in zip(highs, lows, closes, volumes)
    )
    total_v = sum(volumes)
    return total_pv / total_v if total_v else closes[-1]


# ═══════════════════════════════════════════════════════════════════════════════
# OPENING RANGE STATE TRACKER
# ═══════════════════════════════════════════════════════════════════════════════
class OpeningRangeTracker:
    """Tracks the opening range (9:30–10:00 ET) for the current session."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.or_high: Optional[float] = None
        self.or_low:  Optional[float] = None
        self.locked:  bool            = False   # True once OR period ends
        self._date:   Optional[str]   = None

    def update(self, bar_time: datetime, high: float, low: float) -> None:
        """Feed each 5-minute bar; call during 9:30–10:00 ET."""
        bar_date = bar_time.date().isoformat()
        if bar_date != self._date:
            self.reset()
            self._date = bar_date

        et_time = bar_time.astimezone(config.TIMEZONE)
        bar_t   = et_time.time()

        # Only update during the OR window
        if config.SESSION_START <= bar_t < config.OPENING_RANGE_END:
            self.or_high = max(self.or_high or -math.inf, high)
            self.or_low  = min(self.or_low  or math.inf,  low)
            self.locked  = False
        elif bar_t >= config.OPENING_RANGE_END and not self.locked:
            self.locked = True
            logger.info(
                "Opening Range locked: High=%.2f Low=%.2f Range=%.2f pts",
                self.or_high, self.or_low, (self.or_high or 0) - (self.or_low or 0),
            )

    @property
    def range_points(self) -> float:
        if self.or_high is None or self.or_low is None:
            return 0.0
        return self.or_high - self.or_low

    @property
    def is_valid(self) -> bool:
        return (
            self.locked
            and self.or_high is not None
            and self.or_low  is not None
            and self.range_points >= config.OR_MIN_RANGE_POINTS
        )


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
class StrategyEngine:
    """
    Validates signals arriving from TradingView.
    Also exposes helper methods for session timing.
    """

    def __init__(self) -> None:
        self.tz          = config.TIMEZONE
        self.or_tracker  = OpeningRangeTracker()

    # ── session helpers ─────────────────────────────────────────────────────
    def is_trading_session(self, now: datetime) -> bool:
        """True if the current time is within the allowed trading session."""
        et_time = now.astimezone(self.tz).time()
        return config.SESSION_START <= et_time < config.SESSION_END

    def is_entry_allowed(self, now: datetime) -> bool:
        """True if we are past the OR period and can accept new entries."""
        et_time = now.astimezone(self.tz).time()
        return config.OPENING_RANGE_END <= et_time < config.POSITION_CLOSE_TIME

    def is_force_close_time(self, now: datetime) -> bool:
        """True if we must close all positions now (EOD time stop)."""
        et_time = now.astimezone(self.tz).time()
        return et_time >= config.POSITION_CLOSE_TIME

    # ── signal parsing ──────────────────────────────────────────────────────
    def parse_and_validate_signal(self, payload: dict) -> Optional[dict]:
        """
        Parse a TradingView webhook payload and validate every condition.

        Expected payload keys (see Pine Script alert):
            action        : "BUY" | "SELL"
            instrument    : "MGC" | "GC"
            price         : close price of the triggering bar
            atr           : ATR(14) value
            rsi           : RSI(14) value
            adx           : ADX(14) value
            vwap          : session VWAP
            or_high       : OR high
            or_low        : OR low
            volume        : bar volume
            volume_avg    : 20-bar average volume
            signal_type   : "ORB" | "VWAP_MR"
            ema20         : 20 EMA on 15-min chart
            secret        : must match WEBHOOK_SECRET

        Returns a validated signal dict or None if any check fails.
        """
        now = datetime.now(self.tz)

        # ── 0. Shared secret ──────────────────────────────────────────────
        if payload.get("secret", "") != config.WEBHOOK_SECRET:
            logger.warning("Invalid webhook secret — signal rejected")
            return None

        # ── 1. Required fields ────────────────────────────────────────────
        required = ["action", "instrument", "price", "atr", "signal_type"]
        for key in required:
            if key not in payload:
                logger.warning("Missing required field '%s' in signal", key)
                return None

        action      = payload["action"].upper()          # "BUY" | "SELL"
        instrument  = payload["instrument"].upper()
        price       = float(payload["price"])
        atr_val     = float(payload.get("atr", 1.0))
        rsi_val     = float(payload.get("rsi", 50.0))
        adx_val     = float(payload.get("adx", 25.0))
        vwap        = float(payload.get("vwap", price))
        or_high     = float(payload.get("or_high", price))
        or_low      = float(payload.get("or_low", price))
        volume      = float(payload.get("volume", 0.0))
        volume_avg  = float(payload.get("volume_avg", 1.0))
        signal_type = payload.get("signal_type", "ORB").upper()
        ema20       = float(payload.get("ema20", price))

        if action not in ("BUY", "SELL"):
            logger.warning("Invalid action: %s", action)
            return None

        if instrument not in config.INSTRUMENTS:
            logger.warning("Unknown instrument: %s", instrument)
            return None

        # ── 2. Session check ──────────────────────────────────────────────
        if not self.is_entry_allowed(now):
            logger.info("Signal outside entry window — ignored")
            return None

        # ── 3. ATR sanity ─────────────────────────────────────────────────
        if atr_val <= 0.0:
            logger.warning("ATR ≤ 0 — invalid signal")
            return None

        # ── 4. Opening Range Breakout validations ─────────────────────────
        if signal_type == "ORB":
            result = self._validate_orb(
                action, price, or_high, or_low, vwap, rsi_val, adx_val,
                volume, volume_avg, ema20
            )
            if not result:
                return None

        # ── 5. VWAP Mean-Reversion validations ───────────────────────────
        elif signal_type == "VWAP_MR":
            result = self._validate_vwap_mr(
                action, price, vwap, atr_val, adx_val, rsi_val
            )
            if not result:
                return None

        else:
            logger.warning("Unknown signal_type: %s", signal_type)
            return None

        # ── 6. Build validated signal dict ────────────────────────────────
        sl_points = max(
            atr_val * config.SL_ATR_MIN,
            min(atr_val * config.SL_ATR_MAX, atr_val * config.SL_ATR_DEFAULT),
        )

        signal = {
            "action":      action,
            "instrument":  instrument,
            "price":       price,
            "atr":         atr_val,
            "rsi":         rsi_val,
            "adx":         adx_val,
            "vwap":        vwap,
            "or_high":     or_high,
            "or_low":      or_low,
            "ema20":       ema20,
            "sl_points":   round(sl_points, 2),
            "signal_type": signal_type,
        }
        logger.info(
            "✅ Signal validated: %s %s @ %.2f | ATR=%.2f SL_pts=%.2f RSI=%.1f ADX=%.1f",
            action, instrument, price, atr_val, sl_points, rsi_val, adx_val,
        )
        return signal

    # ── ORB validation ──────────────────────────────────────────────────────
    def _validate_orb(
        self,
        action:     str,
        price:      float,
        or_high:    float,
        or_low:     float,
        vwap:       float,
        rsi_val:    float,
        adx_val:    float,
        volume:     float,
        volume_avg: float,
        ema20:      float,
    ) -> bool:
        """Validate an Opening Range Breakout signal."""
        or_range = or_high - or_low

        # ── A. OR range width ──────────────────────────────────────────────
        if or_range < config.OR_MIN_RANGE_POINTS:
            logger.info(
                "ORB rejected: OR range %.2f < min %.2f",
                or_range, config.OR_MIN_RANGE_POINTS,
            )
            return False

        # ── B. Breakout direction ─────────────────────────────────────────
        if action == "BUY":
            if price <= or_high:
                logger.info("ORB LONG rejected: price %.2f not above OR High %.2f", price, or_high)
                return False
        else:  # SELL
            if price >= or_low:
                logger.info("ORB SHORT rejected: price %.2f not below OR Low %.2f", price, or_low)
                return False

        # ── C. VWAP filter ────────────────────────────────────────────────
        if action == "BUY" and price < vwap:
            logger.info("ORB LONG rejected: price %.2f below VWAP %.2f", price, vwap)
            return False
        if action == "SELL" and price > vwap:
            logger.info("ORB SHORT rejected: price %.2f above VWAP %.2f", price, vwap)
            return False

        # ── D. Higher-TF trend bias (EMA20 on 15-min) ────────────────────
        if action == "BUY" and price < ema20:
            logger.info("ORB LONG rejected: price %.2f below 15m EMA20 %.2f", price, ema20)
            return False
        if action == "SELL" and price > ema20:
            logger.info("ORB SHORT rejected: price %.2f above 15m EMA20 %.2f", price, ema20)
            return False

        # ── E. RSI filter (strictly between 45–55) ────────────────────────
        if action == "BUY":
            if not (config.RSI_LONG_MIN < rsi_val < config.RSI_LONG_MAX):
                logger.info(
                    "ORB LONG rejected: RSI %.1f not in [%.0f, %.0f]",
                    rsi_val, config.RSI_LONG_MIN, config.RSI_LONG_MAX,
                )
                return False
        else:
            if not (config.RSI_SHORT_MIN < rsi_val < config.RSI_SHORT_MAX):
                logger.info(
                    "ORB SHORT rejected: RSI %.1f not in [%.0f, %.0f]",
                    rsi_val, config.RSI_SHORT_MIN, config.RSI_SHORT_MAX,
                )
                return False

        # ── F. Volume confirmation ────────────────────────────────────────
        if volume_avg > 0 and volume < volume_avg * config.VOLUME_MULT:
            logger.info(
                "ORB rejected: volume %.0f < %.1f× avg %.0f",
                volume, config.VOLUME_MULT, volume_avg,
            )
            return False

        logger.info("ORB signal passes all filters ✓")
        return True

    # ── VWAP mean-reversion validation ─────────────────────────────────────
    def _validate_vwap_mr(
        self,
        action:  str,
        price:   float,
        vwap:    float,
        atr_val: float,
        adx_val: float,
        rsi_val: float,
    ) -> bool:
        """Validate an optional VWAP mean-reversion signal."""
        deviation = abs(price - vwap)
        min_dev   = atr_val * config.VWAP_MR_ATR_MULT

        # Must be > 2 ATR from VWAP
        if deviation < min_dev:
            logger.info(
                "VWAP MR rejected: deviation %.2f < %.2f (2×ATR)",
                deviation, min_dev,
            )
            return False

        # Direction must be mean-reverting (selling overbought, buying oversold)
        if action == "BUY" and price >= vwap:
            logger.info("VWAP MR LONG rejected: price above VWAP (not oversold)")
            return False
        if action == "SELL" and price <= vwap:
            logger.info("VWAP MR SHORT rejected: price below VWAP (not overbought)")
            return False

        # Market must be range-bound (ADX < threshold)
        if adx_val >= config.VWAP_MR_ADX_MAX:
            logger.info(
                "VWAP MR rejected: ADX %.1f ≥ %.1f (trending, not range-bound)",
                adx_val, config.VWAP_MR_ADX_MAX,
            )
            return False

        # RSI must confirm the mean-reversion setup
        if action == "BUY" and rsi_val >= 45:
            logger.info("VWAP MR LONG rejected: RSI %.1f not oversold enough", rsi_val)
            return False
        if action == "SELL" and rsi_val <= 55:
            logger.info("VWAP MR SHORT rejected: RSI %.1f not overbought enough", rsi_val)
            return False

        logger.info("VWAP MR signal passes all filters ✓")
        return True

    # ── SL / TP helpers ─────────────────────────────────────────────────────
    @staticmethod
    def compute_levels(
        action:     str,
        entry:      float,
        sl_points:  float,
    ) -> Dict[str, float]:
        """Compute SL, TP1, and TP2 prices from entry and ATR-based SL distance."""
        if action == "BUY":
            sl   = entry - sl_points
            tp1  = entry + sl_points * config.TP1_R
            tp2  = entry + sl_points * config.TP2_R
        else:
            sl   = entry + sl_points
            tp1  = entry - sl_points * config.TP1_R
            tp2  = entry - sl_points * config.TP2_R
        return {
            "sl_price":  round(sl,  2),
            "tp1_price": round(tp1, 2),
            "tp2_price": round(tp2, 2),
        }
