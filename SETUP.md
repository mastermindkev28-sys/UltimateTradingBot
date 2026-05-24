# UltimateTradingBot v2 — Complete Setup Guide

**Ultra-Conservative Gold Futures Bot | ORB + VWAP + Optional Order Flow | Tradovate + TradingView**

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Tradovate API Setup](#3-tradovate-api-setup)
4. [Telegram Bot Setup](#4-telegram-bot-setup)
5. [VPS Deployment](#5-vps-deployment)
6. [Environment Configuration](#6-environment-configuration)
7. [TradingView Pine Script Setup](#7-tradingview-pine-script-setup)
8. [Order Flow Filter Setup](#8-order-flow-filter-setup)
9. [Running the Bot](#9-running-the-bot)
10. [Prop Firm Configuration](#10-prop-firm-configuration)
11. [Emergency Procedures](#11-emergency-procedures)
12. [Daily Workflow](#12-daily-workflow)
13. [Risk Management Reference](#13-risk-management-reference)
14. [Troubleshooting](#14-troubleshooting)
---

## 1. Architecture Overview

```
TradingView (5-min GC/MGC chart)
    │
    │  Pine Script: ORB conditions + Order Flow metrics
    │  fires webhook when all primary conditions met
    ▼
POST /webhook  (JSON payload with optional of_* fields)
    │
    ▼
Python Bot (your VPS)
    ├── Webhook Server (FastAPI)        ← receives the signal
    ├── Strategy Validator              ← re-validates ORB/VWAP conditions
    ├── Order Flow Filter  [OPTIONAL]  ← cumulative delta, absorption, imbalance
    ├── News Filter                     ← blocks near high-impact events
    ├── Risk Manager                    ← sizes position, enforces all limits
    ├── Tradovate Client (REST+WS)      ← places bracket orders
    └── Telegram Alerter                ← rich alerts with OF confirmation status
```

**Signal flow for a typical LONG entry (Order Flow enabled):**

1. TradingView bar closes above OR High with VWAP/EMA/RSI/Volume confirmation
2. Pine Script calculates OF metrics (volume delta, absorption, imbalance)
3. Webhook fires → `POST /webhook` with primary + OF fields in JSON payload
4. Bot validates: session ✓ | news clear ✓ | strategy ✓ | **OF filter ✓**
5. Bot sizes position: 0.30% risk / ATR-adjusted SL → e.g. 2 MGC contracts
6. Tradovate receives bracket order: Entry(Market) + TP1(Limit) + SL(Stop)
7. Telegram sends entry notification with OF confirmation details
8. Monitor loop watches equity every 10 seconds for time stops / loss limits

---

## 2. Prerequisites

| Requirement | Detail |
|---|---|
| Python | 3.10 or higher |
| Tradovate account | Live OR Demo (start with Demo) |
| TradingView account | Any paid plan (Pro / Pro+ / Premium) |
| Telegram account | For alert bot |
| VPS | Ubuntu 22.04 LTS recommended |
| Open port | 8080 (or configure your port) |

---

## 3. Tradovate API Setup

### Step 1: Create a Tradovate Account

- Go to [tradovate.com](https://tradovate.com)
- Open a **Simulator** account first (free) — never test with live money
- Fund a paper account or use the simulator balance

### Step 2: Generate API Credentials

1. Log into Tradovate → **Settings** → **API Access**
2. Click **Create Application**
3. Fill in:
   - Application Name: `UltimateTradingBot`
   - Application Version: `1.0`
4. Note down:
   - **App ID** → `TRADOVATE_APP_ID`
   - **CID** (Client ID) → `TRADOVATE_CID`
   - **Secret** → `TRADOVATE_SECRET`
5. Your username and password are your regular login credentials

### Step 3: Verify API Access

```bash
curl -X POST https://demo.tradovateapi.com/v1/auth/accesstokenrequest \
  -H "Content-Type: application/json" \
  -d '{
    "name": "YOUR_USERNAME",
    "password": "YOUR_PASSWORD",
    "appId": "YOUR_APP_ID",
    "appVersion": "1.0",
    "cid": YOUR_CID,
    "sec": "YOUR_SECRET"
  }'
```

You should receive a response containing `"accessToken": "..."`.

---

## 4. Telegram Bot Setup

### Step 1: Create the Bot

1. Open Telegram → search for **@BotFather**
2. Send `/newbot`
3. Follow prompts → note the **bot token** (format: `123456789:AABB...`)

### Step 2: Get Your Chat ID

1. Send any message to your new bot
2. Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id": -1001234567890}` — that is your `TELEGRAM_CHAT_ID`

> **Tip:** For group alerts, add your bot to a private group and use the group's negative ID.

### Step 3: Test the Bot

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d "chat_id=<CHAT_ID>&text=Bot+connected+✅"
```

---

## 5. VPS Deployment

### Recommended Specs (minimal)

- Ubuntu 22.04 LTS
- 1 vCPU, 1 GB RAM
- 20 GB SSD
- Location: US East (low latency to Tradovate servers)

### Providers: DigitalOcean, Vultr, Linode, AWS Lightsail

### Step-by-Step VPS Setup

```bash
# 1. Update system
sudo apt update && sudo apt upgrade -y

# 2. Install Python 3.11
sudo apt install python3.11 python3.11-venv python3.11-pip -y

# 3. Clone / upload the bot
git clone https://github.com/YOUR_REPO/UltimateTradingBot.git
cd UltimateTradingBot

# 4. Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# 5. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 6. Configure environment
cp .env.example .env
nano .env   # fill in all values

# 7. Open the webhook port
sudo ufw allow 8080/tcp
sudo ufw enable
```

### Running as a systemd Service (Recommended)

```bash
# Create service file
sudo nano /etc/systemd/system/tradingbot.service
```

Paste:

```ini
[Unit]
Description=UltimateTradingBot Gold Futures
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/UltimateTradingBot
Environment=PATH=/home/ubuntu/UltimateTradingBot/venv/bin
ExecStart=/home/ubuntu/UltimateTradingBot/venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable tradingbot
sudo systemctl start tradingbot

# Check status
sudo systemctl status tradingbot

# View logs live
sudo journalctl -u tradingbot -f
```

### Nginx Reverse Proxy (Optional — for HTTPS)

```bash
sudo apt install nginx certbot python3-certbot-nginx -y

# Create site config
sudo nano /etc/nginx/sites-available/tradingbot
```

```nginx
server {
    server_name your-domain.com;

    location /webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /health {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/tradingbot /etc/nginx/sites-enabled/
sudo certbot --nginx -d your-domain.com
sudo systemctl restart nginx
```

> With HTTPS, use `https://your-domain.com/webhook` in TradingView alerts.

---

## 6. Environment Configuration

Edit `.env` with your values:

```bash
nano .env
```

**Critical settings:**

| Variable | What it does | Safe default |
|---|---|---|
| `DEMO_MODE` | `true` = paper trading | `true` ✅ |
| `TRADOVATE_*` | API credentials | — |
| `TELEGRAM_*` | Alert bot config | — |
| `WEBHOOK_SECRET` | Must match Pine Script | random 32-char hex |
| `DEFAULT_INSTRUMENT` | `MGC` for eval, `GC` for large accts | `MGC` |
| `RISK_PER_TRADE_PCT` | 0.003 = 0.30% per trade | `0.003` |
| `DAILY_LOSS_LIMIT_PCT` | Hard stop at 0.85% day loss | `0.0085` |
| `PROP_FIRM` | Configures display only | `topstep_50k` |

**Generate a webhook secret:**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## 7. TradingView Pine Script Setup

### Step 1: Add the Indicator

1. Open TradingView → open a **5-minute** chart of **MGC1!** or **GC1!**
2. Click **Pine Editor** (bottom of screen)
3. Paste the contents of `pine_script/gold_orb_strategy.pine`
4. Click **Add to chart**

### Step 2: Configure Indicator Settings

- Symbol: `MGC` (recommended for evaluations)
- Webhook Secret: paste your `WEBHOOK_SECRET` from `.env`
- All other defaults are pre-tuned for the strategy

### Step 3: Create TradingView Alerts

Create **4 separate alerts** (one per signal type):

#### Alert 1 — ORB Long

- **Condition:** Gold-ORB → **ORB Long Entry**
- **Trigger:** Once Per Bar Close
- **Webhook URL:** `http://YOUR_VPS_IP:8080/webhook`
- **Message (replace values):**

```json
{
  "action":      "BUY",
  "instrument":  "MGC",
  "signal_type": "ORB",
  "price":       {{close}},
  "atr":         {{plot("ATR14")}},
  "rsi":         {{plot("RSI14")}},
  "adx":         {{plot("ADX")}},
  "vwap":        {{plot("VWAP")}},
  "ema20":       {{plot("15m EMA20")}},
  "or_high":     {{plot("OR High")}},
  "or_low":      {{plot("OR Low")}},
  "volume":      {{volume}},
  "volume_avg":  {{plot("Vol MA")}},
  "secret":      "YOUR_WEBHOOK_SECRET_HERE"
}
```

#### Alert 2 — ORB Short

Same as Alert 1 but `"action": "SELL"` and condition: **ORB Short Entry**

#### Alert 3 — VWAP MR Long / Short

Use `"signal_type": "VWAP_MR"` with `"action": "BUY"` or `"SELL"`.
All other fields identical.  Note: OF filter is NOT applied to VWAP MR trades.

> ⚠️ **Important:** Replace `YOUR_WEBHOOK_SECRET` in every alert message with your actual `WEBHOOK_SECRET` from `.env`

**For the full webhook JSON templates including Order Flow fields, see the comment block at the bottom of `pine_script/gold_orb_strategy.pine`.**

### Step 4: Test the Webhook (without Order Flow)

```bash
curl -X POST http://YOUR_VPS_IP:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "action": "BUY",
    "instrument": "MGC",
    "signal_type": "ORB",
    "price": 1950.5,
    "atr": 3.2,
    "rsi": 51.0,
    "adx": 18.5,
    "vwap": 1948.0,
    "ema20": 1947.5,
    "or_high": 1949.0,
    "or_low": 1944.0,
    "volume": 2500,
    "volume_avg": 1800,
    "secret": "YOUR_WEBHOOK_SECRET_HERE"
  }'
```

### Step 4b: Test the Webhook (with Order Flow fields)

```bash
curl -X POST http://YOUR_VPS_IP:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "action": "BUY",
    "instrument": "MGC",
    "signal_type": "ORB",
    "price": 1950.5,
    "atr": 3.2,
    "rsi": 51.0,
    "adx": 18.5,
    "vwap": 1948.0,
    "ema20": 1947.5,
    "or_high": 1949.0,
    "or_low": 1944.0,
    "volume": 2500,
    "volume_avg": 1800,
    "of_cumulative_delta": 1250.5,
    "of_bar_volume_delta": 320.0,
    "of_bid_ask_imbalance": 0.35,
    "of_absorption_at_or_low": 1,
    "of_absorption_at_or_high": 0,
    "of_absorption_strength": 0.72,
    "of_delta_divergence": 0,
    "of_source": "approximation",
    "secret": "YOUR_WEBHOOK_SECRET_HERE"
  }'
```

Expected response: `{"status":"accepted"}`

---

## 8. Order Flow Filter Setup

> **Philosophy:** Order flow is a second-layer confirmation filter, never the primary signal source. It is designed to eliminate low-probability breakouts while keeping the strategy simple and reliable.

### What "Order Flow" Means in This Bot

| Metric | What it measures | Data source |
|---|---|---|
| Cumulative Delta | Net buying vs selling for the whole session | Volume delta sum |
| Bar Volume Delta | Net buying on the specific breakout bar | Volume × close position |
| Bid/Ask Imbalance | Bid vs ask pressure (-1 to +1 scale) | Close position in range |
| Absorption at OR Low | Bears tested below OR Low but failed | Wick + volume analysis |
| Absorption at OR High | Bulls tested above OR High but failed | Wick + volume analysis |
| Delta Divergence | Price moving opposite to delta | Price vs delta comparison |

### Data Quality Notes

| Mode | Quality | Requirement |
|---|---|---|
| Bar-range approximation | ⭐⭐⭐ (good directional signal) | Any TradingView plan |
| TradingView Order Flow+ | ⭐⭐⭐⭐⭐ (true bid/ask volume) | Premium + OF+ subscription |
| Tradovate Time & Sales | ⭐⭐⭐⭐⭐ (exchange-direct) | Available via Tradovate API |

The Pine Script defaults to bar-range approximations (Mode A).  This is
sufficient for directional confirmation in liquid Gold futures.

### Recommended Roll-Out Sequence

**Phase 1 — Baseline (weeks 1–2)**
```env
ORDER_FLOW_ENABLED=false
```
Run normally. Build a baseline dataset of signal quality without OF.

**Phase 2 — Soft Filter (weeks 3–4)**
```env
ORDER_FLOW_ENABLED=true
OF_REQUIRE_POSITIVE_DELTA=true
OF_REQUIRE_BAR_DELTA=true
OF_ALLOW_MISSING_DATA=true
```
Enable only the two most reliable conditions. Allow missing data so
no signal is blocked if TradingView doesn't send OF fields.

**Phase 3 — Full Confirmation (after 30+ signals with OF data)**
```env
ORDER_FLOW_ENABLED=true
OF_REQUIRE_POSITIVE_DELTA=true
OF_REQUIRE_BAR_DELTA=true
OF_REQUIRE_NO_DIVERGENCE=true
OF_ALLOW_MISSING_DATA=false
```
Now require OF data. Add divergence filter. Review logs to decide
whether absorption adds value for your specific market conditions.

### Order Flow in Telegram Alerts

When `ORDER_FLOW_ENABLED=true`, entry alerts include:

```
📊 Order Flow Confirmation
  Σ Delta:      +1250   Bar Δ: +320
  Bid/Ask Imb:  +0.35   Source: approximation
  Abs@ORLow:    ✓       Abs@ORHigh: ✗
  ✅ Passed: CumDelta=+1250 > +0, BarDelta=+320 > +0
```

When the filter is disabled, the OF section is omitted from alerts.

### Upgrading to True Order Flow Data

For true footprint/delta data without approximations:

1. **TradingView Order Flow+** (requires Premium subscription)
   - Add a "Volume Profile" or "Order Flow" indicator to your chart
   - Use `request.security()` to pull delta values from that indicator
   - Update the Pine Script's `i_of_use_premium` input to `true`

2. **Tradovate Market Depth API**
   - Access via `GET /md/getDOM` endpoint
   - Returns full bid/ask ladder in real time
   - Can be integrated directly into `tradovate_client.py` for server-side OF calculation

---

## 9. Running the Bot

### Demo / Paper Mode (Start Here)

```bash
# Ensure DEMO_MODE=true in .env
source venv/bin/activate
python main.py
```

Watch the logs:

```
2025-01-15 09:28:00 [INFO    ] main: ✅ Initialisation complete — awaiting signals …
2025-01-15 09:30:00 [INFO    ] main: 📡 Webhook received — action=BUY instrument=MGC
2025-01-15 09:30:00 [INFO    ] main: ✅ Signal validated: BUY MGC @ 1950.5
2025-01-15 09:30:01 [INFO    ] main: ✅ Order placed | orderId=12345
```

### Health Check

```bash
curl http://localhost:8080/health
# → {"status":"ok","mode":"DEMO"}
```

### Emergency Stop (all positions flattened)

```bash
curl -X POST http://localhost:8080/flatten \
  -H "Content-Type: application/json" \
  -d '{"secret":"YOUR_WEBHOOK_SECRET"}'
```

### Pause / Resume

```bash
# Pause
curl -X POST http://localhost:8080/pause \
  -d '{"secret":"...","command":"pause"}'

# Resume
curl -X POST http://localhost:8080/pause \
  -d '{"secret":"...","command":"resume"}'
```

---

## 9. Prop Firm Configuration

### Topstep

| Account | Daily Loss Limit | Max Trailing DD | Profit Target |
|---|---|---|---|
| $50K | $1,000 | $2,000 | $3,000 |
| $100K | $2,000 | $3,000 | $6,000 |
| $150K | $3,000 | $4,500 | $9,000 |

**Recommended `.env` for Topstep $50K:**

```env
PROP_FIRM=topstep_50k
DEFAULT_INSTRUMENT=MGC
RISK_PER_TRADE_PCT=0.003
DAILY_LOSS_LIMIT_PCT=0.008
DAILY_PROFIT_TARGET_PCT=0.010
MAX_TRADES_PER_DAY=3
```

### MyFundedFutures

| Account | Daily Loss | Max DD | Profit Target |
|---|---|---|---|
| $50K | $1,250 | $2,500 | $3,000 |
| $100K | $2,500 | $5,000 | $6,000 |

### Lucid Trading

| Account | Daily Loss | Max DD | Profit Target |
|---|---|---|---|
| $50K | $1,000 | $2,500 | $3,000 |
| $100K | $2,000 | $5,000 | $6,000 |

### Conservative Strategy: Progress in 4–7 Days

Target **1.0–1.2% per day** on good days, skip bad days entirely.

- Day 1–2: 2–3 trades, target ~$300–500 net on $50K
- Day 3–4: Same approach, compound equity
- Day 5–6: Should reach profit target with 50%+ buffer remaining
- Day 7: Typically sufficient to pass with this conservative approach

---

## 10. Emergency Procedures

### One-Click Flatten All (from terminal)

```bash
curl -X POST http://localhost:8080/flatten \
  -H "Content-Type: application/json" \
  -d '{"secret":"YOUR_WEBHOOK_SECRET"}'
```

### Kill the Bot Immediately

```bash
sudo systemctl stop tradingbot
```

### Manual Liquidation via Tradovate

1. Log into [app.tradovate.com](https://app.tradovate.com)
2. Go to **Positions** tab
3. Right-click position → **Liquidate**

### What to do if the bot errors and positions are open

1. **Do NOT panic** — log into Tradovate and close positions manually
2. Check logs: `sudo journalctl -u tradingbot -n 100`
3. Fix the issue, then restart: `sudo systemctl restart tradingbot`

---

## 11. Daily Workflow

### Before Trading (9:00–9:25 AM ET)

- [ ] Check the economic calendar for high-impact events today
- [ ] Add manual events if needed: set `MANUAL_NEWS_TIMES` in `.env`
- [ ] Verify bot is running: `sudo systemctl status tradingbot`
- [ ] Check health endpoint: `curl http://localhost:8080/health`
- [ ] Review overnight global events (gold is affected by geopolitics)

### During Trading (9:30 AM–3:00 PM ET)

- Monitor Telegram for entry/exit alerts
- Never manually interfere unless the bot is clearly malfunctioning
- The bot enforces all rules — trust the system

### After Market Close (3:00 PM ET)

- Telegram sends daily summary automatically at 3:05 PM ET
- Review `logs/daily_reports/YYYY-MM-DD.json` for full trade details
- Update cumulative P&L tracking for prop firm progress

### Log Files

```
logs/
├── trading_bot.log              # rolling application log
└── daily_reports/
    ├── 2025-01-15.json          # today's trades + stats
    └── 2025-01-14.json          # previous days
```

---

## 12. Risk Management Reference

### Position Sizing Formula

```
risk_$       = equity × RISK_PER_TRADE_PCT
cost_1_contract = sl_points × point_value
contracts    = floor(risk_$ / cost_1_contract)

Example (MGC, $50K account, 1×ATR SL, ATR=3.0):
  risk_$       = 50000 × 0.003 = $150
  cost_1_MGC   = 3.0 × $10    = $30
  contracts    = floor(150/30) = 5  → capped at prop_ceiling
```

### Gate Sequence (all must pass)

```
1. Trading session (9:30–15:00 ET)
2. Entry window (10:00–14:45 ET, past OR period)
3. Not paused / not daily-shutdown
4. Daily loss limit not hit
5. Daily profit target not hit (optional stop-early)
6. < MAX_TRADES_PER_DAY
7. < 2 consecutive losses
8. Not in news blackout window
9. No open position in this instrument
10. Strategy validation (OR breakout / VWAP / RSI / volume / ADX)
11. Calculated contracts ≥ 1
12. Consistency rule (no day > 35% of cumulative profits)
```

### Size Reduction Schedule

| Condition | Action |
|---|---|
| Normal | 100% size |
| 1 consecutive loss | 50% size |
| 2 consecutive losses | STOP for the day |

---

## 13. Troubleshooting

### Bot starts but no trades execute

1. Check TradingView alert is configured with **Once Per Bar Close**
2. Verify webhook URL is correct and port 8080 is open
3. Test the webhook manually with curl
4. Check if RSI / volume conditions are filtering out signals (look at logs)

### Orders rejected by Tradovate

- Verify account has sufficient margin
- Check contract name is correct (front month rolls periodically)
- Confirm the account is in **Demo/Simulator** mode if `DEMO_MODE=true`

### "Authentication failed" error

- Double-check all `TRADOVATE_*` values in `.env`
- Ensure the application is approved in your Tradovate account settings
- Try authenticating with the `curl` command from Section 3

### Telegram alerts not sending

- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
- Make sure you have sent at least one message to the bot first
- Check if the chat ID is negative (for groups) — must include the `-` sign

### TradingView alert fires but bot ignores it

Check the logs for which gate rejected the signal:
```bash
grep "Gate" logs/trading_bot.log | tail -20
```

Common causes:
- Signal outside entry window (OR not complete yet)
- RSI not between 45–55
- Volume below threshold
- News blackout window active
- Price not truly above OR High / VWAP

### Port 8080 not accessible

```bash
sudo ufw allow 8080/tcp
sudo ufw status
# Also check cloud provider's security group / firewall rules
```

---

## Support

- Logs are your first debugging tool: `sudo journalctl -u tradingbot -f`
- All decisions are logged with "Gate N FAIL" messages explaining exactly why a signal was rejected
- The bot is designed to be **fail-safe**: when in doubt it does nothing

> **⚠️ Disclaimer:** This software is for educational purposes. Trading futures involves substantial risk. Past performance of a strategy does not guarantee future results. Always paper-trade first and understand every risk parameter before using real capital.
