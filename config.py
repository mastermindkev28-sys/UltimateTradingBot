"""
config.py — Ultra-Conservative Gold Futures Trading Bot  (v2 — with Order Flow)
================================================================================
Central configuration for all modules. Every tuneable parameter lives here.
Override via environment variables or .env file — no hardcoded secrets.

v2 additions vs v1:
  • ORDER_FLOW_* section — optional order flow confirmation module
  • OF conditions are individually togglable (all OFF by default)
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
    point_value:           float   # USD per full point
    tick_size:             float   # Minimum price movement
    tick_value:            float   # USD per tick
    default_max_contracts: int     # Hard ceiling regardless of sizing calc
    tradovate_name:        str     # Symbol as used in Tradovate API

INSTRUMENTS: Dict[str, InstrumentSpec] = {
    "MGC": InstrumentSpec(
        symbol="MGC",
        full_name="Micro Gold Futures",
        point_value=10.0,
        tick_size=0.10,
        tick_value=1.00,
        default_max_contracts=10,
        tradovate_name="MGC",
    ),
    "GC": InstrumentSpec(
        symbol="GC",
        full_name="Gold Futures",
        point_value=100.0,
        tick_size=0.10,
        tick_value=10.00,
        default_max_contracts=2,
        tradovate_name="GC",
    ),
}

DEFAULT_INSTRUMENT = os.getenv("DEFAULT_INSTRUMENT", "MGC").upper()

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION / TIMING  (all times in US/Eastern)
# PST↔ET offset is always 3 hours: 6am PST = 9am ET, 8am PST = 11am ET
# ═══════════════════════════════════════════════════════════════════════════════
TIMEZONE = pytz.timezone("America/New_York")

def _parse_time(env_key: str, default: str) -> time:
    """Parse HH:MM env var into a time object."""
    raw = os.getenv(env_key, default)
    try:
        h, m = map(int, raw.split(":"))
        return time(h, m)
    except Exception:
        h, m = map(int, default.split(":"))
        return time(h, m)

SESSION_START       = _parse_time("SESSION_START_ET", "09:30")
SESSION_END         = _parse_time("SESSION_END_ET",   "15:00")
POSITION_CLOSE_TIME = _parse_time("POSITION_CLOSE_ET", "14:45")
OPENING_RANGE_END   = _parse_time("OPENING_RANGE_END_ET", "10:00")

ENTRY_DELAY_BARS = int(os.getenv("ENTRY_DELAY_BARS", "1"))

# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS — OPENING RANGE BREAKOUT
# ═══════════════════════════════════════════════════════════════════════════════
OR_MIN_RANGE_POINTS = float(os.getenv("OR_MIN_RANGE_POINTS", "1.5"))

VWAP_MR_ATR_MULT    = 2.0
VWAP_MR_ADX_MAX     = 22.0

EMA_PERIOD          = 20
RSI_PERIOD          = 14
RSI_LONG_MIN        = 45
RSI_LONG_MAX        = 55
RSI_SHORT_MIN       = 45
RSI_SHORT_MAX       = 55
ATR_PERIOD          = 14
ADX_PERIOD          = 14
VOLUME_MA_PERIOD    = 20
VOLUME_MULT         = 1.2

# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
RISK_PER_TRADE_PCT  = float(os.getenv("RISK_PER_TRADE_PCT", "0.003"))
RISK_MAX_PCT        = 0.004
RISK_MIN_PCT        = 0.0025

SL_ATR_DEFAULT      = float(os.getenv("SL_ATR_DEFAULT", "1.0"))
SL_ATR_MIN          = 0.75
SL_ATR_MAX          = 1.2

TP1_R               = 1.5
TP2_R               = 2.5

DAILY_LOSS_LIMIT_PCT    = float(os.getenv("DAILY_LOSS_LIMIT_PCT",    "0.0085"))
DAILY_PROFIT_TARGET_PCT = float(os.getenv("DAILY_PROFIT_TARGET_PCT", "0.012"))
# Fixed-dollar profit target — overrides PCT when > 0 (e.g. 500 = stop at +$500)
DAILY_PROFIT_TARGET_USD = float(os.getenv("DAILY_PROFIT_TARGET_USD", "0"))
MAX_TRADES_PER_DAY      = int(os.getenv("MAX_TRADES_PER_DAY", "3"))

MAX_DAY_PROFIT_SHARE        = 0.35
AFTER_1_LOSS_SIZE_REDUCTION = 0.50
AFTER_2_LOSSES_STOP         = True
INTRADAY_DD_PAUSE_PCT       = 0.005
PROP_FIRM_DD_BUFFER_PCT     = 0.50

# ═══════════════════════════════════════════════════════════════════════════════
# ORDER FLOW FILTER  (v2 addition)
# ═══════════════════════════════════════════════════════════════════════════════
# ─── Master switch ─────────────────────────────────────────────────────────────
# Keep OFF during initial prop evaluation phases for maximum reliability.
# Enable one condition at a time after gathering at least 10 days of signal data.
ORDER_FLOW_ENABLED = os.getenv("ORDER_FLOW_ENABLED", "false").lower() in ("true", "1", "yes")

# ─── Individual condition toggles (only relevant when ORDER_FLOW_ENABLED=true) ─
# Require cumulative session delta to agree with trade direction:
#   Long  → cumulative delta must be positive (net buying)
#   Short → cumulative delta must be negative (net selling)
OF_REQUIRE_POSITIVE_DELTA = os.getenv("OF_REQUIRE_POSITIVE_DELTA", "true").lower() in ("true", "1")

# Require the breakout/breakdown bar's volume delta to agree with direction:
#   Long  → bar delta > 0 (buying pressure on the entry bar)
#   Short → bar delta < 0 (selling pressure on the entry bar)
OF_REQUIRE_BAR_DELTA = os.getenv("OF_REQUIRE_BAR_DELTA", "true").lower() in ("true", "1")

# Require bid/ask size imbalance (useful with true Level II data; noisy with approximations)
OF_REQUIRE_IMBALANCE = os.getenv("OF_REQUIRE_IMBALANCE", "false").lower() in ("true", "1")

# Require absorption event at the relevant OR boundary:
#   Long  → absorption at OR Low (bears failed to hold below it)
#   Short → absorption at OR High (bulls failed to hold above it)
OF_REQUIRE_ABSORPTION = os.getenv("OF_REQUIRE_ABSORPTION", "false").lower() in ("true", "1")

# Reject signals where price and cumulative delta diverge (price up but delta down = warning)
OF_REQUIRE_NO_DIVERGENCE = os.getenv("OF_REQUIRE_NO_DIVERGENCE", "false").lower() in ("true", "1")

# ─── Numeric thresholds ────────────────────────────────────────────────────────
# Minimum absolute value of cumulative delta required (0 = any sign agreement)
OF_MIN_CUMULATIVE_DELTA  = float(os.getenv("OF_MIN_CUMULATIVE_DELTA",  "0"))

# Minimum absolute value of bar delta required (0 = any sign agreement)
OF_MIN_BAR_DELTA         = float(os.getenv("OF_MIN_BAR_DELTA",          "0"))

# Minimum bid/ask imbalance (0.0–1.0 scale; 0.2 = moderate bid dominance)
OF_IMBALANCE_THRESHOLD   = float(os.getenv("OF_IMBALANCE_THRESHOLD",    "0.20"))

# Minimum absorption strength score (0.0–1.0; 0.5 = moderate absorption)
OF_MIN_ABSORPTION_STRENGTH = float(os.getenv("OF_MIN_ABSORPTION_STRENGTH", "0.50"))

# Minimum consecutive bars of agreeing delta direction (0 = disabled)
OF_MIN_DELTA_TREND_BARS  = int(os.getenv("OF_MIN_DELTA_TREND_BARS",  "0"))

# ─── Behaviour when OF data is missing from payload ───────────────────────────
# True  = allow signal if no OF fields in payload (fail-open)
# False = reject signal if OF fields expected but missing (fail-safe)
OF_ALLOW_MISSING_DATA = os.getenv("OF_ALLOW_MISSING_DATA", "true").lower() in ("true", "1")

# ═══════════════════════════════════════════════════════════════════════════════
# NEWS FILTER
# ═══════════════════════════════════════════════════════════════════════════════
NEWS_BLACKOUT_BEFORE_MIN = int(os.getenv("NEWS_BLACKOUT_BEFORE_MIN", "45"))
NEWS_BLACKOUT_AFTER_MIN  = int(os.getenv("NEWS_BLACKOUT_AFTER_MIN",  "45"))

HIGH_IMPACT_KEYWORDS: List[str] = [
    "CPI", "Consumer Price Index",
    "FOMC", "Federal Open Market",
    "Fed Chair", "Federal Reserve Chair",
    "NFP", "Non-Farm Payroll",
    "GDP", "Gross Domestic Product",
    "PPI", "Producer Price Index",
    "PCE",
    "Retail Sales",
    "ISM", "PMI",
    "Unemployment Rate",
    "Interest Rate Decision",
    "Fed Funds Rate",
    "Treasury",
    "Inflation",
    "Jackson Hole",
    "Balance Sheet",
]

NEWS_CALENDAR_URL     = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CALENDAR_TIMEOUT = 10

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
POSITION_POLL_INTERVAL = 10
EQUITY_POLL_INTERVAL   = 30
TOKEN_REFRESH_MARGIN   = 3600
MAX_RETRY_ATTEMPTS     = 4
RETRY_BASE_DELAY       = 2.0
