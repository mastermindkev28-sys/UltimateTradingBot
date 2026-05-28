#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════
#  UltimateTradingBot — One-Command Mac Setup
#  Run: bash setup.sh
# ═══════════════════════════════════════════════════════
set -euo pipefail

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

banner() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }
ok()     { echo -e "  ${GREEN}✅ $1${NC}"; }
warn()   { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
die()    { echo -e "\n${RED}❌ $1${NC}\n"; exit 1; }

echo -e "${BOLD}"
echo "  ╔════════════════════════════════════════╗"
echo "  ║   UltimateTradingBot — Setup Wizard    ║"
echo "  ╚════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Python check ─────────────────────────────────────────────────────────
banner "Checking Python …"
if command -v python3 &>/dev/null; then
    PY=$(python3 --version 2>&1)
    ok "Found $PY"
    PYTHON=python3
    PIP=pip3
elif command -v python &>/dev/null; then
    PY=$(python --version 2>&1)
    ok "Found $PY"
    PYTHON=python
    PIP=pip
else
    warn "Python not found. Installing via Homebrew …"
    if ! command -v brew &>/dev/null; then
        echo "  Installing Homebrew first …"
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    brew install python
    PYTHON=python3
    PIP=pip3
fi

# ── 2. Virtual environment ───────────────────────────────────────────────────
banner "Setting up virtual environment …"
if [ ! -d "venv" ]; then
    $PYTHON -m venv venv
    ok "Created venv/"
else
    ok "venv/ already exists — reusing"
fi

source venv/bin/activate
PIP="pip"   # always use venv pip after activation
ok "Virtual environment activated"

# ── 3. Install dependencies ──────────────────────────────────────────────────
banner "Installing Python dependencies …"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
ok "All dependencies installed"

# ── 4. Create .env ───────────────────────────────────────────────────────────
banner "Configuring environment …"

if [ -f ".env" ]; then
    warn ".env already exists — skipping (delete it to reconfigure)"
else
    # Generate random secrets
    WEBHOOK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    DASHBOARD_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")

    echo ""
    echo -e "  ${BOLD}I'll ask for your credentials now.${NC}"
    echo -e "  ${YELLOW}Press Enter to skip anything — you can edit .env later.${NC}"
    echo ""

    read -p "  Tradovate username: " TV_USER
    read -s -p "  Tradovate password: " TV_PASS; echo
    read -p "  Tradovate App ID:   " TV_APP_ID
    read -p "  Tradovate CID:      " TV_CID
    read -s -p "  Tradovate Secret:   " TV_SECRET; echo
    echo ""
    read -p "  Telegram Bot Token (from @BotFather): " TG_TOKEN
    read -p "  Telegram Chat ID (your personal ID):  " TG_CHAT
    read -p "  Signal Channel ID (@Quant_PF_signals_bot channel): " TG_SIG_CHAT
    echo ""
    read -p "  Prop firm [topstep_50k/topstep_100k/mff_50k/lucid_50k] (default: topstep_50k): " PROP_FIRM
    PROP_FIRM="${PROP_FIRM:-topstep_50k}"

    cat > .env <<EOF
# ── Mode ──────────────────────────────────────────────
DEMO_MODE=true

# ── Tradovate ─────────────────────────────────────────
TRADOVATE_USERNAME=${TV_USER}
TRADOVATE_PASSWORD=${TV_PASS}
TRADOVATE_APP_ID=${TV_APP_ID}
TRADOVATE_APP_VERSION=1.0
TRADOVATE_CID=${TV_CID}
TRADOVATE_SECRET=${TV_SECRET}

# ── Telegram Alerts ───────────────────────────────────
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}

# ── Telegram Signal Receiver ──────────────────────────
TELEGRAM_SIGNAL_CHAT_ID=${TG_SIG_CHAT}
TELEGRAM_POLL_TIMEOUT=25

# ── Dashboard ─────────────────────────────────────────
DASHBOARD_PASSWORD=${DASHBOARD_PASS}
DASHBOARD_PORT=8088

# ── Webhook ───────────────────────────────────────────
WEBHOOK_SECRET=${WEBHOOK_SECRET}
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8080

# ── Instrument & Risk ─────────────────────────────────
DEFAULT_INSTRUMENT=MGC
RISK_PER_TRADE_PCT=0.003
DAILY_LOSS_LIMIT_PCT=0.0085
DAILY_PROFIT_TARGET_PCT=0.012
MAX_TRADES_PER_DAY=3
SL_ATR_DEFAULT=1.0
OR_MIN_RANGE_POINTS=1.5
PROP_FIRM=${PROP_FIRM}

# ── News ──────────────────────────────────────────────
NEWS_BLACKOUT_BEFORE_MIN=45
NEWS_BLACKOUT_AFTER_MIN=45
STRICT_NEWS_FILTER=false
MANUAL_NEWS_TIMES=

# ── Order Flow (OFF by default) ───────────────────────
ORDER_FLOW_ENABLED=false
OF_REQUIRE_POSITIVE_DELTA=true
OF_REQUIRE_BAR_DELTA=true
OF_REQUIRE_IMBALANCE=false
OF_REQUIRE_ABSORPTION=false
OF_REQUIRE_NO_DIVERGENCE=false
OF_MIN_CUMULATIVE_DELTA=0
OF_MIN_BAR_DELTA=0
OF_IMBALANCE_THRESHOLD=0.20
OF_MIN_ABSORPTION_STRENGTH=0.50
OF_MIN_DELTA_TREND_BARS=0
OF_ALLOW_MISSING_DATA=true

# ── Logging ───────────────────────────────────────────
LOG_LEVEL=INFO
LOG_FILE=logs/trading_bot.log
DAILY_REPORT_DIR=logs/daily_reports
EOF

    ok ".env created"
    echo ""
    echo -e "  ${BOLD}Your auto-generated dashboard password:${NC}"
    echo -e "  ${GREEN}${BOLD}  ${DASHBOARD_PASS}${NC}"
    echo -e "  ${YELLOW}  (save this somewhere!)${NC}"
fi

# ── 5. Create log dirs ───────────────────────────────────────────────────────
mkdir -p logs/daily_reports static
ok "Log directories ready"

# ── 6. Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✅  Setup complete!${NC}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}To start the bot:${NC}"
echo -e "  ${CYAN}  source venv/bin/activate${NC}"
echo -e "  ${CYAN}  python3 main.py${NC}"
echo ""
echo -e "  ${BOLD}Then open your dashboard:${NC}"
echo -e "  ${CYAN}  http://localhost:8088${NC}"
echo ""
echo -e "  ${YELLOW}Edit credentials anytime:  nano .env${NC}"
echo ""

# ── 7. Optionally start now ──────────────────────────────────────────────────
read -p "  Start demo dashboard now? [y/N]: " START
START_LOWER=$(echo "$START" | tr '[:upper:]' '[:lower:]')
if [[ "$START_LOWER" == "y" ]]; then
    echo ""
    echo -e "  ${GREEN}Starting demo dashboard … (Ctrl+C to stop)${NC}"
    echo -e "  ${CYAN}  Open → http://localhost:8088   Password: demo${NC}"
    echo ""
    python3 demo.py
fi
