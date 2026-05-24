"""
config.py — Ultra-Conservative Gold Futures Trading Bot
========================================================
Central configuration for all modules. Every tuneable parameter lives here.
Override via environment variables or .env file — no hardcoded secrets.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List
from datetime import time

from dotenv import load_dotenv
import pytz

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# SECRETS / CREDENTIALS  (loaded from .env — never commit real values)
# ═══════════════════════════════════════════════════════════════════════════════
TRADOVATE_USERNAME    = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD    = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID      = os.getenv("TRADOVATE_APP_ID", "")
TRADOVATE_APP_VERSION = os.getenv("TRADOVATE_APP_VERSION", "1.0")
TRADOVATE_CID         = os.getenv("TRADOVATE_CID", "")
TRADOVATE_SECRET      = os.getenv("TRADOVATE_SECRET", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "changeme_set_in_env")
WEBHOOK_HOST   = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT   = int(os.getenv("WEBHOOK_PORT", "8080"))

# ═══════════════════════════════════════════════════════════════════════════════
# OPERATING MODE  — DEFAULT IS DEMO/PAPER TRADING
# ═══════════════════════════════════════════════════════════════════════════════
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() in ("true", "1", "yes")

# Tradovate REST base URL and WebSocket URLs
TRADOVATE_BASE_URL = (
    "https://demo.tradovateapi.com/v1"
    if DEMO_MODE
    else "https://live.tradovateapi.com/v1"
)
TRADOVATE_WS_URL = (
    "wss://demo.tradovateapi.com/v1/websocket"
    if DEMO_MODE
    else "wss://live.tradovateapi.com/v1/websocket"
)
TRADOVATE_MD_URL = "wss://md.tradovateapi.com/v1/websocket"

# ═══════════════════════════════════════════════════════════════════════════════
# INSTRUMENT SPECIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class InstrumentSpec:
    symbol:                str
    full_name:             str
    point_value:           float   # USD per full point (e.g. 1.0 = $100 for GC)
    tick_size:             float   # Minimum price movement
    tick_value:            float   # USD per tick
    default_max_contracts: int     # Hard ceiling regardless of sizing calc
    tradovate_name:        str     # Symbol as used in Tradovate API

INSTRUMENTS: Dict[str, InstrumentSpec] = {
    "MGC": InstrumentSpec(
        symbol="MGC",
        full_name="Micro Gold Futures",
        point_value=10.0,      # $10 / point
        tick_size=0.10,
        tick_value=1.00,
        default_max_contracts=10,
        tradovate_name="MGC",
    ),
    "GC": InstrumentSpec(
        symbol="GC",
        full_name="Gold Futures",
        point_value=100.0,     # $100 / point
        tick_size=0.10,
        tick_value=10.00,
        default_max_contracts=2,
        tradovate_name="GC",
    ),
}

# Primary instrument (use MGC for prop firm evaluations — safer sizing)
DEFAULT_INSTRUMENT = os.getenv("DEFAULT_INSTRUMENT", "MGC").upper()

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION / TIMING  (all times in US/Eastern)
# ═══════════════════════════════════════════════════════════════════════════════
TIMEZONE = pytz.timezone("America/New_York")

SESSION_START       = time(9, 30)   # 09:30 ET — session open
SESSION_END         = time(15, 0)   # 15:00 ET — no new entries after this
POSITION_CLOSE_TIME = time(14, 45)  # 14:45 ET — force-close all open positions
OPENING_RANGE_END   = time(10, 0)   # 10:00 ET — OR period complete, entries allowed after

# Minimum bar close time after OR end before first entry (avoid first-bar fakes)
ENTRY_DELAY_BARS = int(os.getenv("ENTRY_DELAY_BARS", "1"))

# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS — OPENING RANGE BREAKOUT
# ═══════════════════════════════════════════════════════════════════════════════

# --- Opening Range ---------------------------------------------------------
OR_MIN_RANGE_POINTS = float(os.getenv("OR_MIN_RANGE_POINTS", "1.5"))
# Skip the day if OR is tighter than this (choppy, no edge)

# --- VWAP Mean-Reversion (optional counter-trend) -------------------------
VWAP_MR_ATR_MULT    = 2.0   # Enter MR only if price > 2× ATR from VWAP
VWAP_MR_ADX_MAX     = 22.0  # Only in range-bound conditions (ADX < 22)

# --- Indicators -----------------------------------------------------------
EMA_PERIOD          = 20    # EMA period (on 15-min chart for trend bias)
RSI_PERIOD          = 14
RSI_LONG_MIN        = 45    # RSI range for LONG entries
RSI_LONG_MAX        = 55
RSI_SHORT_MIN       = 45    # RSI range for SHORT entries
RSI_SHORT_MAX       = 55
ATR_PERIOD          = 14
ADX_PERIOD          = 14
VOLUME_MA_PERIOD    = 20
VOLUME_MULT         = 1.2   # Volume must exceed 1.2× its 20-bar average

# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGEMENT  (all percentages expressed as decimals, e.g. 0.003 = 0.30%)
# ═══════════════════════════════════════════════════════════════════════════════

# --- Per-trade risk -------------------------------------------------------
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.003"))   # 0.30% default
RISK_MAX_PCT       = 0.004   # Hard ceiling — never exceed 0.40%
RISK_MIN_PCT       = 0.0025  # Floor — minimum meaningful risk

# --- Stop-loss / Take-profit (ATR-based) ---------------------------------
SL_ATR_DEFAULT = float(os.getenv("SL_ATR_DEFAULT", "1.0"))  # 1.0 × ATR for SL
SL_ATR_MIN     = 0.75   # Tightest allowed SL (never goes below 0.75 ATR)
SL_ATR_MAX     = 1.2    # Widest allowed SL (never widen beyond 1.2 ATR)

TP1_R = 1.5   # First target = 1.5R (close 50% of position)
TP2_R = 2.5   # Second target = 2.5R (close remaining 50%)

# --- Daily limits ---------------------------------------------------------
DAILY_LOSS_LIMIT_PCT   = float(os.getenv("DAILY_LOSS_LIMIT_PCT",   "0.0085"))  # 0.85%
DAILY_PROFIT_TARGET_PCT = float(os.getenv("DAILY_PROFIT_TARGET_PCT", "0.012")) # 1.20%
MAX_TRADES_PER_DAY     = int(os.getenv("MAX_TRADES_PER_DAY", "3"))

# --- Consistency rule (prop firm safeguard) --------------------------------
MAX_DAY_PROFIT_SHARE   = 0.35   # No single day > 35% of cumulative profits

# --- Consecutive-loss management ------------------------------------------
AFTER_1_LOSS_SIZE_REDUCTION = 0.50   # Cut size by 50% after first loss
AFTER_2_LOSSES_STOP         = True   # Hard stop after 2 consecutive losses

# --- Intraday equity curve protection ------------------------------------
INTRADAY_DD_PAUSE_PCT = 0.005  # Pause if intraday drawdown exceeds 0.50%

# --- Prop-firm buffer (maintained at all times) ---------------------------
PROP_FIRM_DD_BUFFER_PCT = 0.50  # Keep 50% of allowed DD as safety margin

# ═══════════════════════════════════════════════════════════════════════════════
# NEWS FILTER
# ═══════════════════════════════════════════════════════════════════════════════
NEWS_BLACKOUT_BEFORE_MIN = int(os.getenv("NEWS_BLACKOUT_BEFORE_MIN", "45"))
NEWS_BLACKOUT_AFTER_MIN  = int(os.getenv("NEWS_BLACKOUT_AFTER_MIN",  "45"))

# Keywords that trigger a high-impact event blackout
HIGH_IMPACT_KEYWORDS: List[str] = [
    "CPI", "Consumer Price Index",
    "FOMC", "Federal Open Market",
    "Fed Chair", "Federal Reserve Chair",
    "NFP", "Non-Farm Payroll",
    "GDP", "Gross Domestic Product",
    "PPI", "Producer Price Index",
    "PCE",
    "Retail Sales",
    "ISM",
    "PMI",
    "Unemployment Rate",
    "Interest Rate Decision",
    "Fed Funds Rate",
    "Treasury",
    "Inflation",
    "Jackson Hole",
    "Balance Sheet",
]

# Calendar source (Forex Factory public JSON — no API key required)
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CALENDAR_TIMEOUT = 10   # seconds

# ═══════════════════════════════════════════════════════════════════════════════
# PROP FIRM PRESETS
# ═══════════════════════════════════════════════════════════════════════════════
PROP_FIRM_PRESETS: Dict[str, dict] = {
    "topstep_50k":  {"account_size": 50000,  "max_daily_loss": 1000,  "trailing_dd": 2000,  "profit_target": 3000},
    "topstep_100k": {"account_size": 100000, "max_daily_loss": 2000,  "trailing_dd": 3000,  "profit_target": 6000},
    "topstep_150k": {"account_size": 150000, "max_daily_loss": 3000,  "trailing_dd": 4500,  "profit_target": 9000},
    "mff_50k":      {"account_size": 50000,  "max_daily_loss": 1250,  "trailing_dd": 2500,  "profit_target": 3000},
    "mff_100k":     {"account_size": 100000, "max_daily_loss": 2500,  "trailing_dd": 5000,  "profit_target": 6000},
    "lucid_50k":    {"account_size": 50000,  "max_daily_loss": 1000,  "trailing_dd": 2500,  "profit_target": 3000},
    "lucid_100k":   {"account_size": 100000, "max_daily_loss": 2000,  "trailing_dd": 5000,  "profit_target": 6000},
}
PROP_FIRM = os.getenv("PROP_FIRM", "topstep_50k")

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE         = os.getenv("LOG_FILE",  "logs/trading_bot.log")
DAILY_REPORT_DIR = os.getenv("DAILY_REPORT_DIR", "logs/daily_reports")

# ═══════════════════════════════════════════════════════════════════════════════
# POLLING / RETRY
# ═══════════════════════════════════════════════════════════════════════════════
POSITION_POLL_INTERVAL = 10   # seconds between position checks
EQUITY_POLL_INTERVAL   = 30   # seconds between equity refreshes
TOKEN_REFRESH_MARGIN   = 3600 # refresh token 1 hour before expiry
MAX_RETRY_ATTEMPTS     = 4
RETRY_BASE_DELAY       = 2.0  # seconds (doubles each attempt)
