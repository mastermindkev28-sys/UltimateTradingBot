"""
order_flow_filter.py — Optional Order Flow Confirmation Filter
==============================================================
Provides a second-layer confirmation of ORB signals using order flow
metrics.  When enabled, all configured conditions must pass BEFORE an
ORB signal is allowed to proceed to order placement.

IMPORTANT: Order flow is used strictly as CONFIRMATION, never as the
primary signal source.  The primary signal must already pass all ORB
conditions in strategy_logic.py before this module is even invoked.

Data Sources (in priority order)
─────────────────────────────────
1. Webhook payload from TradingView Pine Script
   - Uses bar-range-based volume delta approximations (works on any subscription)
   - Users with TradingView Order Flow+ can send true footprint delta values
2. Tradovate Market Depth API (optional, when available)
   - True bid/ask volume data from the exchange
   - Provides real absorption detection via price ladder analysis

Conditions Available
────────────────────
 • Cumulative Delta Direction   — session-aggregate buyer/seller dominance
 • Bar Volume Delta             — breakout bar shows confirming volume flow
 • Bid/Ask Imbalance            — bid/ask size ratio at the current price
 • Absorption Detection         — failed tests of OR boundaries (wick-based)
 • Delta Divergence Filter      — rejects signals where price and delta disagree

Each condition is individually togglable via config.  The default
configuration (all OFF) keeps the bot in simple, reliable mode suitable
for the initial stages of a prop firm evaluation.

Enable gradually as you collect data and gain confidence in the metrics.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

import config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class OrderFlowData:
    """
    Container for all order flow metrics received via webhook or API.

    All numeric values use normalised units matching the Pine Script output:
      - delta values:    contracts/shares (positive = net buying)
      - imbalance:       -1.0 to +1.0 scale (positive = bid-side dominant)
      - absorption flags: bool (True = absorption event detected)
    """

    # ── Core metrics ─────────────────────────────────────────────────────────
    cumulative_delta:      float = 0.0   # Session cumulative volume delta
    bar_volume_delta:      float = 0.0   # This bar's delta (buy_vol - sell_vol)
    bid_ask_imbalance:     float = 0.0   # Normalized bid/ask imbalance (-1 to +1)

    # ── Absorption flags ──────────────────────────────────────────────────────
    absorption_at_or_low:  bool  = False # Bears tried below OR Low but failed (bullish)
    absorption_at_or_high: bool  = False # Bulls tried above OR High but failed (bearish)
    absorption_strength:   float = 0.0   # 0.0–1.0 score of absorption intensity

    # ── Divergence ────────────────────────────────────────────────────────────
    delta_divergence:      bool  = False # Price and delta diverging (warning flag)
    delta_trend_bars:      int   = 0     # Consecutive bars of agreeing delta direction

    # ── Metadata ──────────────────────────────────────────────────────────────
    available:             bool  = False # True if OF data actually arrived in payload
    source:                str   = ""    # "webhook" | "tradovate_api" | "approximation"


@dataclass
class OrderFlowResult:
    """Result returned by the filter after evaluating a signal."""
    passed:           bool
    reason:           str            # Human-readable pass/fail explanation
    conditions_met:   list = field(default_factory=list)   # Passing conditions
    conditions_failed: list = field(default_factory=list)  # Failing conditions
    of_data:          Optional[OrderFlowData] = None


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER FLOW FILTER
# ═══════════════════════════════════════════════════════════════════════════════
class OrderFlowFilter:
    """
    Evaluates order flow conditions against a trading signal.

    Usage:
        filter = OrderFlowFilter()
        result = filter.validate("BUY", of_data, or_high=1950.0, or_low=1944.0)
        if result.passed:
            ... proceed to order placement ...
    """

    def __init__(self) -> None:
        self.enabled                  = config.ORDER_FLOW_ENABLED
        self.require_positive_delta   = config.OF_REQUIRE_POSITIVE_DELTA
        self.require_bar_delta        = config.OF_REQUIRE_BAR_DELTA
        self.require_imbalance        = config.OF_REQUIRE_IMBALANCE
        self.require_absorption       = config.OF_REQUIRE_ABSORPTION
        self.require_no_divergence    = config.OF_REQUIRE_NO_DIVERGENCE
        self.min_cumulative_delta     = config.OF_MIN_CUMULATIVE_DELTA
        self.min_bar_delta            = config.OF_MIN_BAR_DELTA
        self.imbalance_threshold      = config.OF_IMBALANCE_THRESHOLD
        self.min_absorption_strength  = config.OF_MIN_ABSORPTION_STRENGTH
        self.min_delta_trend_bars     = config.OF_MIN_DELTA_TREND_BARS
        self.allow_missing_data       = config.OF_ALLOW_MISSING_DATA

        # Log the active configuration at startup
        if self.enabled:
            active = []
            if self.require_positive_delta:  active.append("CumDelta")
            if self.require_bar_delta:        active.append("BarDelta")
            if self.require_imbalance:        active.append("BidAskImbalance")
            if self.require_absorption:       active.append("Absorption")
            if self.require_no_divergence:    active.append("NoDivergence")
            logger.info(
                "OrderFlowFilter ENABLED | active conditions: [%s]",
                ", ".join(active) if active else "none (pass-through mode)",
            )
        else:
            logger.info("OrderFlowFilter DISABLED — signals pass without OF confirmation")

    # ── main validation entry point ─────────────────────────────────────────
    def validate(
        self,
        action:   str,           # "BUY" | "SELL"
        of_data:  OrderFlowData,
        or_high:  float = 0.0,
        or_low:   float = 0.0,
    ) -> OrderFlowResult:
        """
        Validate order flow for an entry signal.

        Returns an OrderFlowResult.  When the filter is disabled, always
        returns passed=True with an explanatory reason string.
        """
        if not self.enabled:
            return OrderFlowResult(
                passed = True,
                reason = "Order flow filter disabled — signal passes automatically",
                of_data = of_data,
            )

        # ── Handle missing data ───────────────────────────────────────────
        if not of_data.available:
            if self.allow_missing_data:
                logger.info(
                    "OF data not available in signal — allow_missing_data=True, passing"
                )
                return OrderFlowResult(
                    passed = True,
                    reason = "OF data unavailable — allowed to pass by config",
                    of_data = of_data,
                )
            else:
                return OrderFlowResult(
                    passed = False,
                    reason = "OF data required but not present in signal payload",
                    conditions_failed = ["data_availability"],
                    of_data = of_data,
                )

        # ── Route to direction-specific validation ────────────────────────
        if action == "BUY":
            return self._validate_long(of_data, or_low)
        elif action == "SELL":
            return self._validate_short(of_data, or_high)
        else:
            return OrderFlowResult(
                passed = False,
                reason = f"Unknown action: {action}",
                conditions_failed = ["invalid_action"],
                of_data = of_data,
            )

    # ── LONG validation ─────────────────────────────────────────────────────
    def _validate_long(self, d: OrderFlowData, or_low: float) -> OrderFlowResult:
        """
        Validate order flow conditions for a LONG ORB entry.

        Required pattern:
          ✓ Positive cumulative delta  → buyers controlling the session
          ✓ Positive bar volume delta  → buying pressure on the breakout bar
          ✓ Bid-side imbalance         → bids outnumbering asks
          ✓ Absorption at OR Low       → failed bearish test (optional)
          ✗ No delta divergence        → price and delta agree
        """
        passed   : list[str] = []
        failed   : list[str] = []

        # ── Condition 1: Cumulative Delta ─────────────────────────────────
        if self.require_positive_delta:
            threshold = abs(self.min_cumulative_delta)
            if d.cumulative_delta > threshold:
                passed.append(
                    f"CumDelta={d.cumulative_delta:+.1f} > +{threshold:.1f} ✓"
                )
            else:
                failed.append(
                    f"CumDelta={d.cumulative_delta:+.1f} ≤ +{threshold:.1f} ✗ "
                    f"(need positive session delta for long)"
                )

        # ── Condition 2: Bar Volume Delta ─────────────────────────────────
        if self.require_bar_delta:
            threshold = abs(self.min_bar_delta)
            if d.bar_volume_delta > threshold:
                passed.append(
                    f"BarDelta={d.bar_volume_delta:+.1f} > +{threshold:.1f} ✓"
                )
            else:
                failed.append(
                    f"BarDelta={d.bar_volume_delta:+.1f} ≤ +{threshold:.1f} ✗ "
                    f"(breakout bar not showing buying pressure)"
                )

        # ── Condition 3: Bid/Ask Imbalance ────────────────────────────────
        if self.require_imbalance:
            if d.bid_ask_imbalance >= self.imbalance_threshold:
                passed.append(
                    f"BidImbalance={d.bid_ask_imbalance:.2f} ≥ {self.imbalance_threshold:.2f} ✓"
                )
            else:
                failed.append(
                    f"BidImbalance={d.bid_ask_imbalance:.2f} < {self.imbalance_threshold:.2f} ✗ "
                    f"(insufficient bid-side dominance)"
                )

        # ── Condition 4: Absorption at OR Low ─────────────────────────────
        if self.require_absorption:
            strength_ok = d.absorption_strength >= self.min_absorption_strength
            if d.absorption_at_or_low and strength_ok:
                passed.append(
                    f"AbsorptionAtORLow strength={d.absorption_strength:.2f} ✓ "
                    f"(bulls defended OR Low)"
                )
            else:
                desc = "not detected" if not d.absorption_at_or_low else f"strength={d.absorption_strength:.2f} too weak"
                failed.append(
                    f"AbsorptionAtORLow {desc} ✗ "
                    f"(need failed bearish test of OR Low)"
                )

        # ── Condition 5: No Delta Divergence ──────────────────────────────
        if self.require_no_divergence:
            if not d.delta_divergence:
                passed.append("NoDeltaDivergence ✓ (price and delta aligned)")
            else:
                failed.append(
                    "DeltaDivergence detected ✗ "
                    "(price breaking up but delta falling — warning)"
                )

        # ── Delta trend consistency (supplementary) ───────────────────────
        if self.min_delta_trend_bars > 0:
            if d.delta_trend_bars >= self.min_delta_trend_bars:
                passed.append(
                    f"DeltaTrendBars={d.delta_trend_bars} ≥ {self.min_delta_trend_bars} ✓"
                )
            else:
                failed.append(
                    f"DeltaTrendBars={d.delta_trend_bars} < {self.min_delta_trend_bars} ✗"
                )

        return self._build_result(passed, failed, d)

    # ── SHORT validation ────────────────────────────────────────────────────
    def _validate_short(self, d: OrderFlowData, or_high: float) -> OrderFlowResult:
        """
        Validate order flow conditions for a SHORT ORB entry.

        Required pattern:
          ✓ Negative cumulative delta  → sellers controlling the session
          ✓ Negative bar volume delta  → selling pressure on breakdown bar
          ✓ Ask-side imbalance         → asks outnumbering bids
          ✓ Absorption at OR High      → failed bullish test (optional)
          ✗ No delta divergence        → price and delta agree
        """
        passed   : list[str] = []
        failed   : list[str] = []

        # ── Condition 1: Cumulative Delta ─────────────────────────────────
        if self.require_positive_delta:
            threshold = abs(self.min_cumulative_delta)
            if d.cumulative_delta < -threshold:
                passed.append(
                    f"CumDelta={d.cumulative_delta:+.1f} < -{threshold:.1f} ✓"
                )
            else:
                failed.append(
                    f"CumDelta={d.cumulative_delta:+.1f} ≥ -{threshold:.1f} ✗ "
                    f"(need negative session delta for short)"
                )

        # ── Condition 2: Bar Volume Delta ─────────────────────────────────
        if self.require_bar_delta:
            threshold = abs(self.min_bar_delta)
            if d.bar_volume_delta < -threshold:
                passed.append(
                    f"BarDelta={d.bar_volume_delta:+.1f} < -{threshold:.1f} ✓"
                )
            else:
                failed.append(
                    f"BarDelta={d.bar_volume_delta:+.1f} ≥ -{threshold:.1f} ✗ "
                    f"(breakdown bar not showing selling pressure)"
                )

        # ── Condition 3: Bid/Ask Imbalance ────────────────────────────────
        if self.require_imbalance:
            if d.bid_ask_imbalance <= -self.imbalance_threshold:
                passed.append(
                    f"AskImbalance={d.bid_ask_imbalance:.2f} ≤ -{self.imbalance_threshold:.2f} ✓"
                )
            else:
                failed.append(
                    f"AskImbalance={d.bid_ask_imbalance:.2f} > -{self.imbalance_threshold:.2f} ✗ "
                    f"(insufficient ask-side dominance)"
                )

        # ── Condition 4: Absorption at OR High ────────────────────────────
        if self.require_absorption:
            strength_ok = d.absorption_strength >= self.min_absorption_strength
            if d.absorption_at_or_high and strength_ok:
                passed.append(
                    f"AbsorptionAtORHigh strength={d.absorption_strength:.2f} ✓ "
                    f"(bears defended OR High)"
                )
            else:
                desc = "not detected" if not d.absorption_at_or_high else f"strength={d.absorption_strength:.2f} too weak"
                failed.append(
                    f"AbsorptionAtORHigh {desc} ✗ "
                    f"(need failed bullish test of OR High)"
                )

        # ── Condition 5: No Delta Divergence ──────────────────────────────
        if self.require_no_divergence:
            if not d.delta_divergence:
                passed.append("NoDeltaDivergence ✓ (price and delta aligned)")
            else:
                failed.append(
                    "DeltaDivergence detected ✗ "
                    "(price breaking down but delta rising — warning)"
                )

        # ── Delta trend consistency (supplementary) ───────────────────────
        if self.min_delta_trend_bars > 0:
            if d.delta_trend_bars >= self.min_delta_trend_bars:
                passed.append(
                    f"DeltaTrendBars={d.delta_trend_bars} ≥ {self.min_delta_trend_bars} ✓"
                )
            else:
                failed.append(
                    f"DeltaTrendBars={d.delta_trend_bars} < {self.min_delta_trend_bars} ✗"
                )

        return self._build_result(passed, failed, d)

    # ── result builder ───────────────────────────────────────────────────────
    @staticmethod
    def _build_result(
        passed: list,
        failed: list,
        of_data: OrderFlowData,
    ) -> OrderFlowResult:
        overall = len(failed) == 0

        if overall:
            reason = f"OF filter PASSED ({len(passed)} condition(s) met)"
            logger.info(
                "✅ OrderFlow PASS — %s", " | ".join(passed) if passed else "no conditions required"
            )
        else:
            reason = f"OF filter FAILED — {len(failed)} condition(s) not met: {'; '.join(failed)}"
            logger.info("❌ OrderFlow FAIL — %s", "; ".join(failed))

        return OrderFlowResult(
            passed             = overall,
            reason             = reason,
            conditions_met     = passed,
            conditions_failed  = failed,
            of_data            = of_data,
        )

    # ── payload parser ───────────────────────────────────────────────────────
    @staticmethod
    def from_payload(payload: dict) -> OrderFlowData:
        """
        Extract order flow metrics from a TradingView webhook payload.

        All 'of_*' fields are optional.  If none are present, `available`
        is set to False so the filter can decide how to handle missing data.

        Pine Script approximation notes:
          of_cumulative_delta    — session running sum of bar deltas
          of_bar_volume_delta    — this bar's estimated buy_vol − sell_vol
          of_bid_ask_imbalance   — close position in bar range, normalised −1 to +1
          of_absorption_at_or_low  — wick pierced OR Low but bar closed above it
          of_absorption_at_or_high — wick pierced OR High but bar closed below it
          of_absorption_strength   — large volume + small range score
          of_delta_divergence      — price direction vs delta direction mismatch
          of_delta_trend_bars      — bars of consistently signed delta
        """
        has_of_fields = any(k.startswith("of_") for k in payload)

        return OrderFlowData(
            cumulative_delta      = float(payload.get("of_cumulative_delta",     0.0)),
            bar_volume_delta      = float(payload.get("of_bar_volume_delta",     0.0)),
            bid_ask_imbalance     = float(payload.get("of_bid_ask_imbalance",    0.0)),
            absorption_at_or_low  = bool( payload.get("of_absorption_at_or_low",  False)),
            absorption_at_or_high = bool( payload.get("of_absorption_at_or_high", False)),
            absorption_strength   = float(payload.get("of_absorption_strength",  0.0)),
            delta_divergence      = bool( payload.get("of_delta_divergence",       False)),
            delta_trend_bars      = int(  payload.get("of_delta_trend_bars",       0)),
            available             = has_of_fields,
            source                = payload.get("of_source", "webhook" if has_of_fields else ""),
        )

    # ── summary string ───────────────────────────────────────────────────────
    @staticmethod
    def format_of_summary(of_data: OrderFlowData) -> str:
        """One-line summary of OF metrics for Telegram alerts and logs."""
        if not of_data.available:
            return "OF: N/A"
        return (
            f"OF: Δ={of_data.cumulative_delta:+.0f} "
            f"barΔ={of_data.bar_volume_delta:+.0f} "
            f"imb={of_data.bid_ask_imbalance:+.2f} "
            f"absLow={'✓' if of_data.absorption_at_or_low else '✗'} "
            f"absHigh={'✓' if of_data.absorption_at_or_high else '✗'}"
        )
