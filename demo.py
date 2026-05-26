"""
demo.py — Dashboard Demo Mode (no Tradovate account needed)
============================================================
Runs the full web dashboard with simulated live data so you can
explore every tab, try the controls, and see how the bot looks in
action — without any broker credentials.

Simulates:
  • Equity curve with realistic intraday movement
  • Open trade that appears / closes
  • Trade history with wins and losses
  • Signal log with pass/fail signals
  • News events
  • Live log stream

Run:
    python3 demo.py

Then open:  http://localhost:8088
Password:   demo
"""

import asyncio
import logging
import math
import os
import random
import sys
from datetime import datetime, timedelta

import pytz

# ── point at the bot package ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
os.makedirs("logs/daily_reports", exist_ok=True)
os.makedirs("static", exist_ok=True)

# Minimal env so config.py loads without real credentials
os.environ.setdefault("TRADOVATE_USERNAME",  "demo")
os.environ.setdefault("TRADOVATE_PASSWORD",  "demo")
os.environ.setdefault("TRADOVATE_APP_ID",    "demo")
os.environ.setdefault("TRADOVATE_CID",       "1")
os.environ.setdefault("TRADOVATE_SECRET",    "demo")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",  "0:demo")
os.environ.setdefault("TELEGRAM_CHAT_ID",    "0")
os.environ.setdefault("WEBHOOK_SECRET",      "demo" * 16)
os.environ.setdefault("DASHBOARD_PASSWORD",  "demo")
os.environ.setdefault("DASHBOARD_PORT",      "8088")
os.environ.setdefault("DEMO_MODE",           "true")
os.environ.setdefault("LOG_LEVEL",           "INFO")
os.environ.setdefault("LOG_FILE",            "logs/demo.log")
os.environ.setdefault("DAILY_REPORT_DIR",    "logs/daily_reports")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("demo")

from bot_state import BotState
from dashboard_server import DashboardServer


# ═══════════════════════════════════════════════════════════════════════════════
# SIMULATION
# ═══════════════════════════════════════════════════════════════════════════════
class DemoSimulator:
    """Feeds realistic fake data into BotState every few seconds."""

    def __init__(self, state: BotState) -> None:
        self.state     = state
        self.tz        = pytz.timezone("America/New_York")
        self.equity    = 50_000.0
        self.session_start = 50_000.0
        self.high_water    = 50_000.0
        self.tick          = 0
        self._open_trade_tick = -1

        # pre-load some trade history
        self._seed_history()
        self._seed_signals()
        self._seed_news()

    # ── seeds ────────────────────────────────────────────────────────────────
    def _seed_history(self) -> None:
        now = datetime.now(self.tz)
        trades = [
            ("BUY",  "MGC", 2, 2341.2, 2335.0, 2347.0, 2353.0, +126.0,  "win",  "ORB"),
            ("SELL", "MGC", 2, 2358.7, 2364.5, 2352.5, 2346.0, -117.0,  "loss", "ORB"),
            ("BUY",  "MGC", 1, 2349.0, 2343.5, 2355.0, 2361.0,  +60.0,  "win",  "ORB"),
            ("BUY",  "GC",  1, 2367.4, 2361.0, 2373.5, 2380.0, +162.0,  "win",  "VWAP_MR"),
            ("SELL", "MGC", 2, 2371.2, 2377.0, 2365.0, 2358.5, +196.0,  "win",  "ORB"),
        ]
        for i, (act, inst, qty, entry, sl, tp1, tp2, pnl, status, sig) in enumerate(trades):
            t = now - timedelta(minutes=90 - i * 15)
            self.state.trade_history.append({
                "action":      act,
                "instrument":  inst,
                "contracts":   qty,
                "entry_price": entry,
                "sl_price":    sl,
                "tp1_price":   tp1,
                "tp2_price":   tp2,
                "pnl":         pnl,
                "status":      status,
                "signal_type": sig,
                "entry_time":  t.isoformat(),
                "exit_time":   (t + timedelta(minutes=12)).isoformat(),
                "of_summary":  "ΔCum:+312 ΔBar:+88 Abs:✓" if status=="win" else "ΔCum:-201 ΔBar:-55",
            })

        self.state.winning_trades = 4
        self.state.losing_trades  = 1
        self.state.daily_trades   = 5
        self.state.consecutive_losses = 0

    def _seed_signals(self) -> None:
        now = datetime.now(self.tz)
        signals = [
            ("BUY",  "MGC", "ORB",     2341.2, True,  "ALL_PASS",        "ΔCum:+312"),
            ("SELL", "MGC", "ORB",     2352.0, False, "Gate1:SessionTime",""),
            ("BUY",  "MGC", "ORB",     2349.0, True,  "ALL_PASS",        "ΔCum:+88"),
            ("SELL", "MGC", "ORB",     2371.2, False, "Gate8:News",      ""),
            ("BUY",  "GC",  "VWAP_MR", 2367.4, True,  "ALL_PASS",        "ΔCum:+201"),
            ("SELL", "MGC", "ORB",     2378.0, False, "Gate10:Strategy", ""),
        ]
        for i, (act, inst, stype, price, passed, gate, of_sum) in enumerate(signals):
            t = now - timedelta(minutes=80 - i * 12)
            self.state._signals.append({
                "t":           t.strftime("%H:%M:%S"),
                "action":      act,
                "instrument":  inst,
                "signal_type": stype,
                "price":       price,
                "passed":      passed,
                "gate":        gate,
                "of_summary":  of_sum,
                "source":      "tradingview",
            })

    def _seed_news(self) -> None:
        now = datetime.now(self.tz)
        self.state.update_news(
            events=[
                {"title": "ISM Manufacturing PMI", "impact": "High",
                 "time": (now + timedelta(minutes=35)).strftime("%H:%M ET")},
                {"title": "Fed Chair Powell Speech", "impact": "High",
                 "time": (now + timedelta(hours=2, minutes=10)).strftime("%H:%M ET")},
            ],
            blackout=False,
            next_min=35,
        )

    # ── main simulation tick ─────────────────────────────────────────────────
    async def tick_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            self.tick += 1
            self._update_equity()
            self._maybe_open_close_trade()
            self._add_log_line()
            await self.state.broadcast()

    def _update_equity(self) -> None:
        # Realistic intraday walk: small drift + mean-reversion + random noise
        drift  = 0.4                            # slight upward bias
        noise  = random.gauss(0, 18)            # tick noise
        revert = (self.session_start - self.equity) * 0.015  # mean-revert to start
        self.equity = max(48_000, self.equity + drift + noise + revert)
        self.high_water = max(self.high_water, self.equity)
        self.state.update_equity(self.equity, self.session_start, self.high_water)

    def _maybe_open_close_trade(self) -> None:
        # Open a fake trade every ~40 ticks, close it after ~12 ticks
        if self.state.open_trade is None and self.tick % 40 == 5:
            now = datetime.now(self.tz)
            price = round(self.equity / 10 * random.uniform(0.98, 1.02), 1)
            action = random.choice(["BUY", "SELL"])
            self.state.open_trade = {
                "action":      action,
                "instrument":  "MGC",
                "contracts":   2,
                "entry_price": price,
                "sl_price":    round(price + (-6 if action=="BUY" else 6), 1),
                "tp1_price":   round(price + (9  if action=="BUY" else -9), 1),
                "tp2_price":   round(price + (15 if action=="BUY" else -15), 1),
                "risk_amount": 30.0,
                "risk_pct":    0.003,
                "entry_time":  now.isoformat(),
                "signal_type": "ORB",
                "of_summary":  "ΔCum:+247 ΔBar:+91 Abs:✓",
                "status":      "open",
            }
            self._open_trade_tick = self.tick
            self.state.add_signal_log(
                action=action, instrument="MGC", signal_type="ORB",
                price=price, passed=True, gate="ALL_PASS",
                of_summary="ΔCum:+247 ΔBar:+91", source="tradingview",
            )

        elif self.state.open_trade and (self.tick - self._open_trade_tick) >= 12:
            trade = dict(self.state.open_trade)
            pnl   = round(random.uniform(-40, 120), 2)
            trade["pnl"]    = pnl
            trade["status"] = "win" if pnl > 0 else "loss"
            self.state.record_trade_closed(trade)
            if pnl > 0:
                self.state.winning_trades += 1
            else:
                self.state.losing_trades  += 1
            self.state.daily_trades += 1

    def _add_log_line(self) -> None:
        messages = [
            ("INFO",    "Monitor loop: equity=$%.2f  pnl=%.2f%%" % (
                self.equity, (self.equity - self.session_start) / self.session_start * 100)),
            ("INFO",    "News calendar: next event in %d min" % max(1, 35 - self.tick // 3)),
            ("DEBUG",   "WebSocket heartbeat OK"),
            ("INFO",    "Waiting for TradingView signal …"),
            ("INFO",    "Order flow filter: OF disabled — passthrough"),
            ("WARNING", "Intraday drawdown: %.2f%%" % (
                (self.high_water - self.equity) / self.high_water * 100)),
        ]
        level, msg = random.choice(messages)
        self.state.add_log(level, msg)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
async def _amain() -> None:
    state = BotState.get()

    # ── Prime state ───────────────────────────────────────────────────────────
    state.set_running(True)
    state.is_paused         = False
    state.is_shutdown_today = False
    state.demo_mode         = True
    state.instrument        = "MGC"
    state.prop_firm         = "topstep_50k"
    state.account_id        = 999999

    sim = DemoSimulator(state)
    sim._update_equity()   # set initial equity immediately

    # ── Start dashboard ───────────────────────────────────────────────────────
    dashboard = DashboardServer(state)
    dashboard.start()

    port = int(os.getenv("DASHBOARD_PORT", "8088"))
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║        UltimateTradingBot — DEMO MODE            ║")
    print("  ╠══════════════════════════════════════════════════╣")
    print(f"  ║  Dashboard →  http://localhost:{port}              ║")
    print("  ║  Password  →  demo                              ║")
    print("  ║                                                  ║")
    print("  ║  Ctrl+C to stop                                  ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()

    await sim.tick_loop()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n  Demo stopped.")


if __name__ == "__main__":
    main()
