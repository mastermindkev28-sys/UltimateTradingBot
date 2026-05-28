"""
demo.py — Prop Firm Evaluation Simulator (no broker account needed)
====================================================================
Simulates a Topstep $50K evaluation with 100% realistic mechanics:

  Prop Firm Rules (Topstep $50K):
    • Starting balance:    $50,000
    • Daily loss limit:    -$1,000/day  (hard stop, resets each morning)
    • Trailing drawdown:   $2,000 from high-water mark
    • Profit target:       $3,000  (+6% = $53,000)
    • Min trading days:    10
    • Consistency rule:    no single day > 30% of total profits
    • Max contracts:       5 MGC

  MGC Trading Reality:
    • Price range:         ~$2,350–$2,450
    • Point value:         $10 / full point
    • Tick:                $1 / 0.1-point tick
    • Commission:          $4.68 round-trip per contract (~$23 on 5 ct)
    • SL:                  2.0 pts ($20/ct) — below OR boundary
    • TP1:                 3.0 pts ($30/ct) at 1.5R — scale out 50%
    • TP2:                 5.0 pts ($50/ct) at 2.5R — remainder
    • Session:             9:00–11:00 ET (6–8 AM PST)
    • OR window:           9:00–9:30 ET (first 30 min)

  Pre-seeded with 3 realistic completed trading days so you can see
  evaluation progress from Day 1.

Run:
    python3 demo.py
Open: http://localhost:8088   Password: demo
"""

import asyncio
import logging
import os
import random
import sys
from datetime import datetime, timedelta
from datetime import time as dtime

import pytz

sys.path.insert(0, os.path.dirname(__file__))
os.makedirs("logs/daily_reports", exist_ok=True)
os.makedirs("static", exist_ok=True)

# ── Env defaults (no real credentials needed) ────────────────────────────────
for k, v in {
    "TRADOVATE_USERNAME": "demo", "TRADOVATE_PASSWORD": "demo",
    "TRADOVATE_APP_ID": "demo",   "TRADOVATE_CID": "1",
    "TRADOVATE_SECRET": "demo",   "TELEGRAM_BOT_TOKEN": "0:demo",
    "TELEGRAM_CHAT_ID": "0",      "WEBHOOK_SECRET": "demo" * 16,
    "DASHBOARD_PASSWORD": "demo", "DASHBOARD_PORT": "8088",
    "DEMO_MODE": "true",          "LOG_LEVEL": "WARNING",
    "LOG_FILE": "logs/demo.log",  "DAILY_REPORT_DIR": "logs/daily_reports",
    "SESSION_START_ET": "09:00",  "SESSION_END_ET": "11:00",
    "POSITION_CLOSE_ET": "10:50", "OPENING_RANGE_END_ET": "09:30",
    "DAILY_PROFIT_TARGET_USD": "750",
}.items():
    os.environ.setdefault(k, v)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("demo")

from bot_state import BotState
from dashboard_server import DashboardServer


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS — Topstep $50K evaluation rules
# ═══════════════════════════════════════════════════════════════════════════════
EVAL_START_BALANCE  = 50_000.0
EVAL_PROFIT_TARGET  =  3_000.0   # $3,000 to pass
EVAL_DAILY_DD_LIMIT =  1_000.0   # max loss any single day
EVAL_TRAILING_DD    =  2_000.0   # trailing from high-water
EVAL_MIN_DAYS       = 10
EVAL_CONSISTENCY    = 0.30       # no day > 30% of total profits
EVAL_MAX_CONTRACTS  = 5

# MGC instrument specs
MGC_POINT_VALUE     = 10.0       # $10 per full point
MGC_TICK            = 0.10       # min price move
COMMISSION_PER_CT   = 4.68       # round-trip per contract
SL_POINTS           = 2.0
TP1_POINTS          = 3.0        # 1.5R
TP2_POINTS          = 5.0        # 2.5R
BASE_PRICE          = 2385.0     # approximate current MGC price

# Risk sizing: 0.3% of equity
RISK_PCT            = 0.003


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE OUTCOME ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def size_contracts(equity: float) -> int:
    """Risk-based position sizing, capped at EVAL_MAX_CONTRACTS."""
    risk_usd   = equity * RISK_PCT
    risk_per_ct = SL_POINTS * MGC_POINT_VALUE
    raw = int(risk_usd / risk_per_ct)
    return max(1, min(raw, EVAL_MAX_CONTRACTS))


def trade_outcome(contracts: int, action: str, entry: float) -> dict:
    """
    Simulate a realistic trade outcome with correct MGC P&L math.

    Outcome distribution (based on ORB research):
      • 35% — Full win: TP1 (50%) + TP2 (50%) both hit
      • 25% — Partial win: TP1 hit, remainder closed at breakeven
      • 40% — Full loss: SL hit on all contracts
    """
    direction  = 1 if action == "BUY" else -1
    commission = contracts * COMMISSION_PER_CT

    sl_price  = round(entry - direction * SL_POINTS,  1)
    tp1_price = round(entry + direction * TP1_POINTS, 1)
    tp2_price = round(entry + direction * TP2_POINTS, 1)

    ct1 = max(1, contracts // 2)   # scaled out at TP1
    ct2 = contracts - ct1           # held to TP2 / BE

    roll = random.random()
    if roll < 0.35:                 # full win
        pnl = (ct1 * TP1_POINTS + ct2 * TP2_POINTS) * MGC_POINT_VALUE - commission
        result = "win"; exit_price = tp2_price
    elif roll < 0.60:               # partial win (TP1 + BE)
        pnl = ct1 * TP1_POINTS * MGC_POINT_VALUE - commission
        result = "win"; exit_price = tp1_price
    else:                           # full loss
        pnl = -contracts * SL_POINTS * MGC_POINT_VALUE - commission
        result = "loss"; exit_price = sl_price

    # Add small slippage noise
    pnl = round(pnl + random.gauss(0, 1.5), 2)

    return {
        "result":      result,
        "pnl":         pnl,
        "exit_price":  exit_price,
        "sl_price":    sl_price,
        "tp1_price":   tp1_price,
        "tp2_price":   tp2_price,
        "commission":  round(commission, 2),
        "contracts":   contracts,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PROP FIRM EVALUATION SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════════
class PropFirmSimulator:
    """
    Full Topstep $50K evaluation simulation.
    Tracks multi-day equity, drawdown, consistency, and evaluation progress.
    """

    def __init__(self, state: BotState) -> None:
        self.state      = state
        self.tz         = pytz.timezone("America/New_York")

        # ── Evaluation-level tracking ─────────────────────────────────────────
        self.eval_equity    = EVAL_START_BALANCE
        self.eval_high_water = EVAL_START_BALANCE
        self.eval_days_pnl: list[float] = []   # each completed day's net P&L
        self.days_traded    = 0

        # ── Current session ───────────────────────────────────────────────────
        self.session_start  = self.eval_equity
        self.equity         = self.eval_equity
        self.high_water     = self.eval_equity

        # ── Simulation state ──────────────────────────────────────────────────
        self.tick               = 0
        self._open_trade_tick   = -1
        self._or_high           = 0.0
        self._or_low            = 0.0
        self._or_locked         = False
        self._price             = BASE_PRICE
        self._daily_loss_hit    = False

        # ── Seed multi-day history ────────────────────────────────────────────
        self._seed_evaluation_history()
        self._seed_news()

    # ── Multi-day history seed ────────────────────────────────────────────────
    def _seed_evaluation_history(self) -> None:
        """
        Pre-load 3 completed trading days with realistic MGC trades.
        This shows evaluation progress from the start.
        """
        now = datetime.now(self.tz)
        base_price = BASE_PRICE - 8.0   # slightly lower on earlier days

        # Day 1: 2 trades, solid win day
        day1_trades = [
            ("BUY",  5, base_price + 0.3, "ORB",     True,  "win",  +176.60, "ΔCum:+412 ΔBar:+134 Abs:✓"),
            ("BUY",  5, base_price + 2.1, "ORB",     True,  "win",  + 51.60, "ΔCum:+287 ΔBar:+89"),
        ]
        # Day 2: 2 trades, loss day (controlled)
        day2_trades = [
            ("SELL", 5, base_price + 4.5, "ORB",     True,  "loss", -123.40, "ΔCum:-201 ΔBar:-67"),
            ("BUY",  4, base_price + 1.8, "VWAP_MR", True,  "win",  + 51.20, "ΔCum:+156 ΔBar:+41"),
        ]
        # Day 3: 3 trades, strong day
        day3_trades = [
            ("BUY",  5, base_price + 3.2, "ORB",     True,  "win",  +176.60, "ΔCum:+523 ΔBar:+178 Abs:✓"),
            ("SELL", 5, base_price + 5.8, "ORB",     False, "loss", -123.40, "ΔCum:-312 ΔBar:-98"),
            ("BUY",  5, base_price + 2.9, "ORB",     True,  "win",  +176.60, "ΔCum:+441 ΔBar:+143 Abs:✓"),
        ]

        all_days = [day1_trades, day2_trades, day3_trades]
        day_pnls = []

        for day_idx, day_trades in enumerate(all_days):
            day_pnl = 0.0
            day_offset = (3 - day_idx)   # days ago
            for trade_idx, (act, qty, entry, sig, passed, result, pnl, of_sum) in enumerate(day_trades):
                direction  = 1 if act == "BUY" else -1
                sl_price   = round(entry - direction * SL_POINTS, 1)
                tp1_price  = round(entry + direction * TP1_POINTS, 1)
                tp2_price  = round(entry + direction * TP2_POINTS, 1)
                commission = qty * COMMISSION_PER_CT

                trade_time = now.replace(hour=9, minute=30) - timedelta(days=day_offset) \
                             + timedelta(minutes=trade_idx * 35)
                exit_time  = trade_time + timedelta(minutes=18)

                self.state.trade_history.append({
                    "action":       act,
                    "instrument":   "MGC",
                    "contracts":    qty,
                    "entry_price":  entry,
                    "sl_price":     sl_price,
                    "tp1_price":    tp1_price,
                    "tp2_price":    tp2_price,
                    "pnl":          pnl,
                    "commission":   round(commission, 2),
                    "status":       result,
                    "signal_type":  sig,
                    "entry_time":   trade_time.isoformat(),
                    "exit_time":    exit_time.isoformat(),
                    "of_summary":   of_sum,
                    "risk_amount":  round(qty * SL_POINTS * MGC_POINT_VALUE, 2),
                    "risk_pct":     RISK_PCT,
                })

                if passed:
                    self.state.add_signal_log(
                        action=act, instrument="MGC", signal_type=sig,
                        price=entry, passed=(result == "win"),
                        gate="ALL_PASS" if result == "win" else "Gate10:Strategy",
                        of_summary=of_sum, source="tradingview",
                    )

                day_pnl += pnl

            day_pnls.append(day_pnl)
            self.eval_equity = round(self.eval_equity + day_pnl, 2)
            self.eval_high_water = max(self.eval_high_water, self.eval_equity)

        self.eval_days_pnl = day_pnls
        self.days_traded   = len(all_days)

        # Today starts where evaluation left off
        self.session_start  = self.eval_equity
        self.equity         = self.eval_equity
        self.high_water     = self.eval_equity

        # Update trade counters
        wins   = sum(1 for t in self.state.trade_history if t["status"] == "win")
        losses = sum(1 for t in self.state.trade_history if t["status"] == "loss")
        self.state.winning_trades     = wins
        self.state.losing_trades      = losses
        self.state.daily_trades       = 0        # today starts fresh
        self.state.consecutive_losses = 0

        # Push initial equity to state
        self.state.update_equity(self.equity, self.session_start, self.high_water)

    def _seed_news(self) -> None:
        now = datetime.now(self.tz)
        self.state.update_news(
            events=[
                {"title": "ISM Manufacturing PMI",   "impact": "High",
                 "time": (now + timedelta(minutes=38)).strftime("%H:%M ET")},
                {"title": "Fed Chair Powell Remarks", "impact": "High",
                 "time": (now + timedelta(hours=1, minutes=55)).strftime("%H:%M ET")},
            ],
            blackout=False,
            next_min=38,
        )

    # ── Price simulation ──────────────────────────────────────────────────────
    def _next_price(self) -> float:
        """Realistic MGC tick-level price movement."""
        # Small random walk with slight mean reversion to BASE_PRICE
        move   = random.gauss(0, 0.3)
        revert = (BASE_PRICE - self._price) * 0.005
        self._price = round(self._price + move + revert, 1)
        return self._price

    def _build_or(self) -> None:
        """Lock the opening range after the OR period."""
        if self._or_locked:
            return
        prices = [self._next_price() for _ in range(6)]
        self._or_high = max(prices) + round(random.uniform(0.2, 0.8), 1)
        self._or_low  = min(prices) - round(random.uniform(0.2, 0.8), 1)
        self._or_locked = True
        self.state.add_log("INFO",
            f"OR locked | High: {self._or_high:.1f}  Low: {self._or_low:.1f}  "
            f"Range: {self._or_high - self._or_low:.1f} pts")

    # ── Live unrealized P&L ───────────────────────────────────────────────────
    def _update_unrealized(self) -> None:
        """Move equity slightly to reflect open trade's live P&L."""
        if not self.state.open_trade:
            return
        trade  = self.state.open_trade
        price  = self._next_price()
        qty    = trade["contracts"]
        sign   = 1 if trade["action"] == "BUY" else -1
        unreal = sign * (price - trade["entry_price"]) * qty * MGC_POINT_VALUE
        live_equity = self.session_start + unreal + self._closed_pnl_today
        self.high_water = max(self.high_water, live_equity)
        self.state.update_equity(
            round(live_equity, 2), self.session_start, self.high_water
        )

    @property
    def _closed_pnl_today(self) -> float:
        return self.equity - self.session_start

    # ── Trade open / close ────────────────────────────────────────────────────
    def _maybe_trade(self) -> None:
        if self.state.is_paused or self._daily_loss_hit:
            return
        if self.state.daily_trades >= 3:
            return

        # Open a trade
        if self.state.open_trade is None:
            if not self._or_locked:
                self._build_or()
                return
            # Fire a breakout signal every ~25 ticks
            if self.tick % 25 != 7:
                return

            action  = random.choice(["BUY", "SELL"])
            entry   = self._or_high + 0.1 if action == "BUY" else self._or_low - 0.1
            qty     = size_contracts(self.equity)
            now     = datetime.now(self.tz)

            outcome = trade_outcome(qty, action, entry)

            trade = {
                "action":       action,
                "instrument":   "MGC",
                "contracts":    qty,
                "entry_price":  round(entry, 1),
                "sl_price":     outcome["sl_price"],
                "tp1_price":    outcome["tp1_price"],
                "tp2_price":    outcome["tp2_price"],
                "risk_amount":  round(qty * SL_POINTS * MGC_POINT_VALUE, 2),
                "risk_pct":     RISK_PCT,
                "commission":   outcome["commission"],
                "entry_time":   now.isoformat(),
                "signal_type":  "ORB",
                "of_summary":   f"ΔCum:{'+' if action=='BUY' else '-'}"
                                f"{random.randint(180,520)} ΔBar:{'+' if action=='BUY' else '-'}"
                                f"{random.randint(60,180)}" +
                                (" Abs:✓" if random.random() > 0.5 else ""),
                "status":       "open",
                "_outcome":     outcome,   # stored, revealed on close
            }
            self.state.record_open_trade(trade)
            self.state.daily_trades += 1
            self._open_trade_tick = self.tick

            self.state.add_signal_log(
                action=action, instrument="MGC", signal_type="ORB",
                price=round(entry, 1), passed=True, gate="ALL_PASS",
                of_summary=trade["of_summary"], source="tradingview",
            )
            self.state.add_log("INFO",
                f"📈 {action} {qty}×MGC @ {entry:.1f} | "
                f"SL={outcome['sl_price']:.1f}  TP1={outcome['tp1_price']:.1f}  "
                f"TP2={outcome['tp2_price']:.1f} | Risk=${qty*SL_POINTS*MGC_POINT_VALUE:.0f}"
                f" + ${outcome['commission']:.2f} commission")

        # Close after ~15 ticks (~30 seconds demo time)
        elif (self.tick - self._open_trade_tick) >= 15:
            self._close_trade()

    def _close_trade(self) -> None:
        if not self.state.open_trade:
            return
        trade   = dict(self.state.open_trade)
        outcome = trade.pop("_outcome", None)
        if outcome is None:
            return

        pnl    = outcome["pnl"]
        result = outcome["result"]

        trade["pnl"]        = pnl
        trade["status"]     = result
        trade["exit_price"] = outcome["exit_price"]
        trade["exit_time"]  = datetime.now(self.tz).isoformat()

        # Update equity (real P&L, not unrealized)
        self.equity = round(self.equity + pnl, 2)
        self.high_water = max(self.high_water, self.equity)

        self.state.record_trade_closed(trade)
        self.state.update_equity(self.equity, self.session_start, self.high_water)

        if result == "win":
            self.state.winning_trades      += 1
            self.state.consecutive_losses   = 0
        else:
            self.state.losing_trades       += 1
            self.state.consecutive_losses  += 1

        sign_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        self.state.add_log(
            "INFO" if result == "win" else "WARNING",
            f"{'✅ WIN' if result=='win' else '❌ LOSS'} {sign_str} | "
            f"Day P&L: ${self.equity - self.session_start:+.2f} | "
            f"Eval P&L: ${self.eval_equity - EVAL_START_BALANCE + (self.equity - self.session_start):+.2f}"
        )

        # Check daily loss limit
        daily_dd = self.session_start - self.equity
        if daily_dd >= EVAL_DAILY_DD_LIMIT:
            self._daily_loss_hit        = True
            self.state.is_shutdown_today = True
            self.state.add_log("WARNING",
                f"🚨 DAILY LOSS LIMIT HIT: -${daily_dd:.2f} — session locked")

    # ── Evaluation status ─────────────────────────────────────────────────────
    def eval_status(self) -> dict:
        today_pnl  = self.equity - self.session_start
        total_pnl  = (self.eval_equity - EVAL_START_BALANCE) + today_pnl
        trailing_floor = max(
            EVAL_START_BALANCE - EVAL_TRAILING_DD,
            self.eval_high_water - EVAL_TRAILING_DD
        )
        drawdown_used = max(0.0, (self.eval_high_water + today_pnl) - self.equity - today_pnl)

        all_day_pnls = self.eval_days_pnl + ([today_pnl] if today_pnl != 0 else [])
        max_day_pnl  = max(all_day_pnls) if all_day_pnls else 0

        if total_pnl > 0:
            consistency_pct = max_day_pnl / total_pnl if total_pnl > 0 else 0
        else:
            consistency_pct = 0

        status = "ON TRACK"
        if total_pnl >= EVAL_PROFIT_TARGET:
            status = "✅ PASSING"
        elif (self.session_start - self.equity) >= EVAL_DAILY_DD_LIMIT:
            status = "⛔ DAILY LIMIT"
        elif self.eval_equity <= trailing_floor:
            status = "❌ FAILED (DD)"
        elif consistency_pct > EVAL_CONSISTENCY:
            status = "⚠️ CONSISTENCY"

        return {
            "total_pnl":       round(total_pnl, 2),
            "profit_target":   EVAL_PROFIT_TARGET,
            "progress_pct":    round(min(100, total_pnl / EVAL_PROFIT_TARGET * 100), 1),
            "today_pnl":       round(today_pnl, 2),
            "daily_dd_limit":  EVAL_DAILY_DD_LIMIT,
            "daily_dd_used":   round(max(0, self.session_start - self.equity), 2),
            "trailing_floor":  round(trailing_floor, 2),
            "days_traded":     self.days_traded + (1 if self.state.daily_trades > 0 else 0),
            "days_needed":     EVAL_MIN_DAYS,
            "consistency_pct": round(consistency_pct * 100, 1),
            "max_contracts":   EVAL_MAX_CONTRACTS,
            "status":          status,
        }

    # ── Periodic log lines ────────────────────────────────────────────────────
    def _log_line(self) -> None:
        ev    = self.eval_status()
        pnl   = ev["today_pnl"]
        total = ev["total_pnl"]
        msgs  = [
            ("INFO",    f"Monitor | Equity=${self.equity:,.2f}  Day={pnl:+.2f}  "
                        f"Eval={total:+.2f}  ({ev['progress_pct']:.1f}% to target)"),
            ("INFO",    f"OR locked | High:{self._or_high:.1f}  Low:{self._or_low:.1f}  "
                        f"Watching for breakout …" if self._or_locked else "Forming OR …"),
            ("DEBUG",   "Tradovate WS heartbeat OK"),
            ("INFO",    f"Consecutive losses: {self.state.consecutive_losses} | "
                        f"Trades today: {self.state.daily_trades}/3"),
            ("INFO",    f"DD buffer: ${self.session_start - self.equity:.2f} used "
                        f"of ${EVAL_DAILY_DD_LIMIT:.0f} daily limit"),
            ("INFO",    f"Eval status: {ev['status']} | Days: "
                        f"{ev['days_traded']}/{ev['days_needed']} minimum"),
        ]
        level, msg = random.choice(msgs)
        self.state.add_log(level, msg)


# ═══════════════════════════════════════════════════════════════════════════════
# SESSION DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════════
os.environ.setdefault("SESSION_START_ET",        "09:00")
os.environ.setdefault("SESSION_END_ET",          "11:00")
os.environ.setdefault("POSITION_CLOSE_ET",       "10:50")
os.environ.setdefault("OPENING_RANGE_END_ET",    "09:30")
os.environ.setdefault("DAILY_PROFIT_TARGET_USD", "750")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
async def _amain() -> None:
    import config as _cfg

    state = BotState.get()
    tz_et  = pytz.timezone("America/New_York")
    tz_pst = pytz.timezone("America/Los_Angeles")

    session_start_et = os.getenv("SESSION_START_ET", "09:00")
    session_end_et   = os.getenv("SESSION_END_ET",   "11:00")
    start_h, start_m = map(int, session_start_et.split(":"))
    end_h,   end_m   = map(int, session_end_et.split(":"))
    session_open  = dtime(start_h, start_m)
    session_close = dtime(end_h, end_m)
    current_t     = datetime.now(tz_et).time()

    # ── Session window gating ─────────────────────────────────────────────────
    if current_t < session_open:
        wait_sec = ((start_h * 60 + start_m) -
                    (current_t.hour * 60 + current_t.minute)) * 60 - current_t.second
        now_pst = datetime.now(tz_pst)
        print(f"\n  ⏳  Waiting for session open: {session_start_et} ET "
              f"({start_h-3:02d}:{start_m:02d} PST)")
        print(f"     Now: {now_pst.strftime('%I:%M %p PST')} | "
              f"Opens in {wait_sec//60}m {wait_sec%60}s\n")
        await asyncio.sleep(wait_sec)
    elif current_t >= session_close:
        print(f"\n  ⛔  Session closed for today ({session_end_et} ET). "
              f"Come back tomorrow.\n")
        return

    # ── Initialise state ──────────────────────────────────────────────────────
    state.set_running(True)
    state.is_paused         = False
    state.is_shutdown_today = False
    state.demo_mode         = True
    state.instrument        = "MGC"
    state.prop_firm         = "topstep_50k"
    state.account_id        = 999999

    sim = PropFirmSimulator(state)

    # ── Start dashboard ───────────────────────────────────────────────────────
    dashboard = DashboardServer(state)
    dashboard.start()

    port    = int(os.getenv("DASHBOARD_PORT", "8088"))
    ev      = sim.eval_status()
    print()
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║     UltimateTradingBot — Topstep $50K Evaluation Demo   ║")
    print("  ╠══════════════════════════════════════════════════════════╣")
    print(f"  ║  Dashboard   →  http://localhost:{port}                  ║")
    print("  ║  Password    →  demo                                    ║")
    print(f"  ║  Evaluation  →  ${ev['total_pnl']:+.2f} of ${EVAL_PROFIT_TARGET:.0f} target "
          f"({ev['progress_pct']:.1f}%)        ║")
    print(f"  ║  Days traded →  {ev['days_traded']} of {ev['days_needed']} minimum required"
          f"                    ║")
    print(f"  ║  Session     →  {session_start_et}–{session_end_et} ET "
          f"({start_h-3:02d}:00–{end_h-3:02d}:00 PST)           ║")
    print(f"  ║  Status      →  {ev['status']:<44} ║")
    print("  ║  Ctrl+C to stop                                        ║")
    print("  ╚══════════════════════════════════════════════════════════╝")
    print()

    _stop = False

    class _DemoBot:
        def stop(self): nonlocal _stop; _stop = True
    state.bot_ref = _DemoBot()

    profit_target = float(os.getenv("DAILY_PROFIT_TARGET_USD", "750"))

    # ── Main loop ─────────────────────────────────────────────────────────────
    while not _stop:
        await asyncio.sleep(2)

        # Drain command queue
        while not state.command_queue.empty():
            try:
                cmd = state.command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            command = cmd.get("_command", "")
            if command == "pause":
                state.is_paused = True
                state.add_log("INFO", "⏸ Bot PAUSED via dashboard")
            elif command == "resume":
                state.is_paused = False
                state.add_log("INFO", "▶️ Bot RESUMED via dashboard")
            elif command == "flatten":
                if state.open_trade:
                    sim._close_trade()
                state.open_positions = []
                state.add_log("WARNING", "🚨 FLATTEN ALL — positions closed")
            elif command == "stop":
                state.add_log("INFO", "🛑 Stop received")
                _stop = True

        if _stop:
            break

        if state.is_paused:
            await state.broadcast()
            continue

        sim.tick += 1

        # Update unrealized P&L while trade is open
        if state.open_trade:
            sim._update_unrealized()
        else:
            sim._maybe_trade()

        # Close trade after hold period
        if state.open_trade and (sim.tick - sim._open_trade_tick) >= 15:
            sim._close_trade()

        # Periodic log line every 5 ticks
        if sim.tick % 5 == 0:
            sim._log_line()

        # Use live profit target from config
        live_target = _cfg.DAILY_PROFIT_TARGET_USD if _cfg.DAILY_PROFIT_TARGET_USD > 0 \
                      else sim.session_start * _cfg.DAILY_PROFIT_TARGET_PCT
        today_pnl = sim.equity - sim.session_start

        await state.broadcast()

        if today_pnl >= live_target:
            state.is_shutdown_today = True
            state.add_log("INFO",
                f"🎯 Daily target hit: +${today_pnl:.2f} — session closed")
            await state.broadcast()
            print(f"\n  🎯  Daily profit target hit: +${today_pnl:.2f}\n")
            break

        if datetime.now(tz_et).time() >= session_close:
            state.add_log("INFO", f"⏰ {session_end_et} ET — session closed")
            await state.broadcast()
            print(f"\n  ⏰  Session ended ({session_end_et} ET)\n")
            break

    dashboard.stop()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n  Demo stopped.")


if __name__ == "__main__":
    main()
