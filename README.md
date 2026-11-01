# STRADDLECORE: NIFTY 50 Algorithmic Trading Studio & Live Execution Engine

A production-grade algorithmic trading and backtesting studio designed for Indian index derivatives (NSE: NIFTY 50). Features a high-performance backtesting engine, an interactive dark-mode dashboard, live broker API integration (Groww & Zerodha Kite), and an automated execution daemon.

---

## 📌 Strategy Architecture

The core automated strategy operates on a **4-Branch Parallel Execution Graph** on NIFTY 50 current weekly expiry options:

```
                      [START NODE: 10:00 AM IST (Mon-Fri)]
                                        │
        ┌───────────────────┬───────────┴───────────┬───────────────────┐
        ▼                   ▼                       ▼                   ▼
   [BRANCH 1]          [BRANCH 2]              [BRANCH 3]          [BRANCH 4]
   Sell ATM CE         Sell ATM PE          Portfolio Guard       Time Exit
   • 20% Stop-Loss     • 20% Stop-Loss      • Every 10s check    • 3:25 PM IST
   • Auto Square-Off   • Auto Square-Off    • Target: +₹15,000   • Square off
        │                   │               • Stop:   -₹15,000     all open
   (If SL Hits)        (If SL Hits)                 │              positions
        │                   │               (If Guard Hit)              │
   Square Off ALL      Square Off ALL               │                   │
   Positions (inc PE)  Positions (inc CE)   Square Off ALL &            │
        │                   │               Halt for the Day            │
   Wait 15 Minutes     Wait 15 Minutes              │                   │
        │                   │                       ▼                   ▼
   Re-enter Straddle   Re-enter Straddle        [COMPLETED]         [COMPLETED]
   (1 Lot CE + PE)     (1 Lot CE + PE)
   (No SL / Target)    (No SL / Target)
```

### Key Parameters:
* **Contract Lot Size:** **`65` Qty** (Official NSE NIFTY 50 contract size).
  * 10 Lots = 650 CE + 650 PE
  * 1 Lot = 65 CE + 65 PE
* **Schedule:** Starts Monday to Friday at **10:00 AM IST**; Mandatory square-off at **3:25 PM IST (15:25)**.
* **Stop-Loss:** 20% on each individual leg premium.
* **Re-entry:** After an SL hit, wait 15 minutes, then re-enter 1 lot ATM straddle (both CE and PE) with no stop-loss or profit target.
* **Portfolio P&L Guard:** Evaluates total combined P&L across all legs every 10 seconds. Default target: `+₹15,000`, stop: `-₹15,000` (automatically scaled proportionally for smaller retail accounts).

---

## 💰 Multi-Strategy Architecture & Capital Sizing

The studio provides position sizing tailored to account balance, with full backtesting and live execution support across 6 production strategies:

| Strategy | Target Capital | Position Sizing | Risk / Margin Mechanism |
| :--- | :--- | :--- | :--- |
| **Hedged Selling (Iron Butterfly)** | **₹30,000 – ₹40,000** | 1 Lot (65 CE / 65 PE) | Buys +300 OTM protective wings first to compress NSE margin from ₹1.25L to ~₹30k–₹35k. Captures intraday theta decay safely within retail capital. |
| **10:12 Bearish Put Breakdown** | **₹30,000** | 1 Lot (65 PE) | Monitors 9:15–10:06 AM opening range. If signal candle at 10:09 AM closes bearish below range midpoint, buys ATM Put at 10:12 AM with 30% SL and 1:2 R:R target. |
| **Nifty Gap Drive Momentum** | **₹30,000** | 1 Lot (65 CE / 65 PE) | Detects overnight gap (>20 pts). Enters directional ATM option at 09:45 AM if opening range confirms gap direction; range-based SL and trailing profit lock. |
| **1 PM Volatility Drop** | **₹30,000** | 1 Lot (65 PE) | Exploits the post-lunch European market open volatility surge between 1:00 PM and 1:30 PM. 3 consecutive drop candles trigger ATM Put entry. |
| **Option Buying (Long Straddle)** | **₹30,000** | 1 Lot (65 CE / 65 PE) | Pure option buying. Zero margin required; maximum downside risk is strictly capped at premium paid. |
| **Classic Short Straddle** | **₹5,00,000 – ₹10,00,000+** | 10 Lots (650 CE / 650 PE) | Institutional naked short straddle execution. Highest win rate and theta capture. |

---

## 🔌 Live Broker API Integration

The studio includes native connector interfaces for Indian discount brokers:

* **Groww Trading API (`growwapi`):**
  - Real-time order placement (`place_order`), modification, and cancellation for NSE F&O.
  - Multi-account switching:
    - **Demo Account:** Simulated ₹30,000 virtual balance for paper trading and validation.
    - **Original / Live Account:** Live order placement directly into the user's Groww account with auto-filled ₹30,000 capital and live balance sync (available margin fetched directly from Groww API).
* **Zerodha Kite Connect v3:**
  - WebSocket quote feed and automated order routing.
* **Credentials Storage:**
  - Securely persisted locally in `cache/broker_config.json`.

---

## 📂 Project Structure

```
doinggg/
├── app.py                         # Flask Web Server & REST API endpoints
├── backtest_cli.py                # Command-Line Backtester with Rich terminal formatting
├── paper_trader.py                # Standalone paper trading daemon
├── cache/
│   ├── broker_config.json         # Persisted broker credentials (Groww / Kite)
│   └── live_algo_state.json       # Real-time state of the automated execution engine
├── engine/
│   ├── data_feed.py               # Historical & intraday NIFTY 5-minute candle provider
│   ├── live_runner.py             # Asynchronous execution daemon (watches clock, routes orders)
│   └── straddle_engine.py         # 4-Branch Straddle Simulation Engine with Black-Scholes pricing
├── static/
│   ├── css/
│   │   └── style.css              # Custom dark-mode design system & animations
│   └── js/
│       └── app.js                 # Interactive dashboard controller, charts, & API client
└── templates/
    └── index.html                 # Algorithmic Trading Studio Dashboard HTML template
```

---

## 🚀 Quickstart & Running Locally

### 1. Prerequisites
Python 3.10+ with virtual environment installed.

### 2. Activate Virtual Environment
```bash
source .venv/bin/activate
```

### 3. Launch the Web Trading Studio
```bash
python app.py
```
Open your browser and navigate to:
```
http://127.0.0.1:5001
```

### 4. Run CLI Backtests
```bash
# 1-Year backtest with 10 lots (Institutional Naked Straddle)
python backtest_cli.py --period "1 year" --capital 500000 --initial-lots 10 --lot-size 65

# 6-Month backtest with 1 lot for ₹40,000 capital
python backtest_cli.py --period "6 months" --capital 40000 --initial-lots 1 --lot-size 65
```

---

## ⚙️ REST API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/strategies` | `GET` | Returns catalog of all supported trading strategies with metadata. |
| `/api/backtest` | `POST` | Executes dynamic backtests for any chosen strategy with custom capital, period, and lots. |
| `/api/day_detail/<date>` | `GET` | Fetches bar-by-bar intraday timeline and P&L curve for a specific day. |
| `/api/export_csv` | `GET` | Exports complete session-by-session trade logs to CSV (descending date order). |
| `/api/broker/config` | `GET / POST` | Reads or updates broker configuration (Groww / Kite). |
| `/api/broker/balance` | `GET` | Fetches real-time available margin balance from Groww API. |
| `/api/broker/test` | `POST` | Authenticates and tests connection latency to broker API. |
| `/api/algo/start` | `POST` | Arms and starts the background live automation engine. |
| `/api/algo/stop` | `POST` | Disarms and stops the automation engine. |
| `/api/algo/square_off_all` | `POST` | Immediately squares off all open positions at market price. |
| `/api/algo/status` | `GET` | Returns real-time execution state, positions, and schedule status. |
| `/api/algo/test_cycle` | `POST` | Executes an instant 1-session paper execution cycle for verification. |

---

## 🔮 Future Roadmap

1. **Multi-Strategy Selector:** Expanding the studio to include additional high-probability strategies (Opening Range Breakout, 1 PM Volatility Drop, Gap Drive).
2. **Live Margin Balance Sync:** Automatic polling of live funds/margin available directly from Groww API.
3. **Telegram / WhatsApp Webhook Alerts:** Real-time push notifications for order fills, SL triggers, and EOD P&L summaries.

## Telegram signal alerts on GitHub Actions

`.github/workflows/signals.yml` runs `github_signals.py` every weekday (8:50 AM–3:40 PM IST, two runs).
It checks all 6 strategies and sends Telegram alerts for entries, exits and a 3:30 PM summary. No orders are placed.

- **Settings:** edit `signals_config.json` (`enabled`, `capital`, `strategies`).
- **Secrets:** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the repository's Actions secrets.
- **Status page:** GitHub Pages serves `docs/index.html` (today's signals, paper P&L history from `docs/data/`).
