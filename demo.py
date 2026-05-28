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
        pnl = self.equity - self.session_start
        messages = [
            ("INFO",    "Monitor loop: equity=$%.2f  pnl=$%.2f" % (self.equity, pnl)),
            ("INFO",    "News calendar: next event in %d min" % max(1, 35 - self.tick // 3)),
            ("DEBUG",   "WebSocket heartbeat OK"),
            ("INFO",    "Waiting for TradingView signal …"),
            ("INFO",    "Order flow filter: OF disabled — passthrough"),
            ("INFO",    "Session window: %s–%s ET | profit target: $%.0f" % (
                os.getenv("SESSION_START_ET", "09:00"),
                os.getenv("SESSION_END_ET",   "11:00"),
                float(os.getenv("DAILY_PROFIT_TARGET_USD", "500")),
            )),
        ]
        level, msg = random.choice(messages)
        self.state.add_log(level, msg)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

# Session defaults: 6am–8am PST = 9am–11am ET
os.environ.setdefault("SESSION_START_ET",          "09:00")
os.environ.setdefault("SESSION_END_ET",            "11:00")
os.environ.setdefault("POSITION_CLOSE_ET",         "10:50")
os.environ.setdefault("OPENING_RANGE_END_ET",      "09:30")
os.environ.setdefault("DAILY_PROFIT_TARGET_USD",   "500")


async def _amain() -> None:
    import config as _cfg

    state = BotState.get()

    profit_target = float(os.getenv("DAILY_PROFIT_TARGET_USD", "500"))
    session_start_et = os.getenv("SESSION_START_ET", "09:00")
    session_end_et   = os.getenv("SESSION_END_ET",   "11:00")
    tz_et  = pytz.timezone("America/New_York")
    tz_pst = pytz.timezone("America/Los_Angeles")

    # ── Wait for session open if outside window ───────────────────────────────
    now_et = datetime.now(tz_et)
    start_h, start_m = map(int, session_start_et.split(":"))
    end_h,   end_m   = map(int, session_end_et.split(":"))
    from datetime import time as dtime
    session_open  = dtime(start_h, start_m)
    session_close = dtime(end_h,   end_m)
    current_t     = now_et.time()

    if current_t < session_open:
        wait_sec = (
            (start_h * 60 + start_m) - (current_t.hour * 60 + current_t.minute)
        ) * 60 - current_t.second
        now_pst = datetime.now(tz_pst)
        print(f"\n  ⏳  Outside session window — waiting until "
              f"{session_start_et} ET ({start_h - 3:02d}:{start_m:02d} PST)")
        print(f"     Current time: {now_pst.strftime('%I:%M %p')} PST "
              f"| Opens in {wait_sec // 60}m {wait_sec % 60}s")
        print("     (Ctrl+C to cancel)\n")
        await asyncio.sleep(wait_sec)
    elif current_t >= session_close:
        print(f"\n  ⛔  Session already closed for today "
              f"({session_end_et} ET / {end_h - 3:02d}:{end_m:02d} PST).")
        print("     Come back tomorrow or adjust SESSION_END_ET in .env\n")
        return

    # ── Prime state ───────────────────────────────────────────────────────────
    state.set_running(True)
    state.is_paused         = False
    state.is_shutdown_today = False
    state.demo_mode         = True
    state.instrument        = "MGC"
    state.prop_firm         = "topstep_50k"
    state.account_id        = 999999

    sim = DemoSimulator(state)
    sim._update_equity()

    # ── Start dashboard ───────────────────────────────────────────────────────
    dashboard = DashboardServer(state)
    dashboard.start()

    now_pst = datetime.now(tz_pst)
    port    = int(os.getenv("DASHBOARD_PORT", "8088"))
    print()
    print("  ╔═════════════════════════════════════════════════════╗")
    print("  ║        UltimateTradingBot — DEMO MODE               ║")
    print("  ╠═════════════════════════════════════════════════════╣")
    print(f"  ║  Dashboard  →  http://localhost:{port}               ║")
    print("  ║  Password   →  demo                                 ║")
    print(f"  ║  Session    →  {session_start_et} – {session_end_et} ET "
          f"({start_h-3:02d}:00 – {end_h-3:02d}:00 PST)   ║")
    print(f"  ║  Auto-close →  +${profit_target:.0f} profit target               ║")
    print("  ║                                                     ║")
    print("  ║  Ctrl+C to stop                                     ║")
    print("  ╚═════════════════════════════════════════════════════╝")
    print()

    import config as _cfg
    _stop = False

    # Give dashboard a way to trigger stop
    class _DemoBot:
        def stop(self): nonlocal _stop; _stop = True
    state.bot_ref = _DemoBot()

    # ── Tick loop with session + profit-target checks ─────────────────────────
    while not _stop:
        await asyncio.sleep(2)

        # ── Drain command queue (makes dashboard controls work) ───────────────
        while not state.command_queue.empty():
            try:
                cmd = state.command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            command = cmd.get("_command", "")
            if command == "pause":
                state.is_paused = True
                state.add_log("INFO", "⏸ Bot PAUSED via dashboard")
                print("  ⏸  Paused")
            elif command == "resume":
                state.is_paused = False
                state.add_log("INFO", "▶️ Bot RESUMED via dashboard")
                print("  ▶️  Resumed")
            elif command == "flatten":
                state.open_trade     = None
                state.open_positions = []
                state.add_log("WARNING", "🚨 FLATTEN ALL — operator command")
                print("  🚨  Flatten all")
            elif command == "stop":
                state.add_log("INFO", "🛑 Stop command received")
                print("  🛑  Stop received — shutting down demo")
                _stop = True

        if _stop:
            break

        # Skip simulation ticks while paused (controls still work)
        if state.is_paused:
            await state.broadcast()
            continue

        sim.tick += 1
        sim._update_equity()
        sim._maybe_open_close_trade()
        sim._add_log_line()

        # Use live profit target from config (dashboard edits take effect here)
        profit_target = _cfg.DAILY_PROFIT_TARGET_USD if _cfg.DAILY_PROFIT_TARGET_USD > 0 \
                        else sim.session_start * _cfg.DAILY_PROFIT_TARGET_PCT

        await state.broadcast()

        # Auto-close at profit target
        daily_pnl = sim.equity - sim.session_start
        if daily_pnl >= profit_target:
            state.is_shutdown_today = True
            state.add_log("INFO",
                f"🎯 Daily profit target reached: +${daily_pnl:.2f} — session closed")
            await state.broadcast()
            print(f"\n  🎯  Profit target hit: +${daily_pnl:.2f} — demo auto-stopped.\n")
            break

        # Auto-close at session end
        now_et2 = datetime.now(tz_et)
        if now_et2.time() >= session_close:
            state.add_log("INFO",
                f"⏰ Session end {session_end_et} ET reached — closing")
            await state.broadcast()
            print(f"\n  ⏰  Session closed ({session_end_et} ET / "
                  f"{end_h-3:02d}:{end_m:02d} PST) — demo stopped.\n")
            break

    dashboard.stop()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n  Demo stopped.")


if __name__ == "__main__":
    main()
