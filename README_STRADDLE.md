# NIFTY 50 Short-Premium ATM Straddle Strategy & Backtest Studio

## Strategy Overview
An institutional-grade intraday option selling system on **NIFTY 50** options (current weekly expiry) featuring parallel branches, individual leg stop-losses with auto square-off, post-stop-loss re-entries, portfolio-level P&L guards, and fixed time exits.

---

## 4 Parallel Branches Architecture

```
                    ┌────────────────────────────────────────┐
                    │       START NODE: 10:00 AM IST         │
                    │   NIFTY 50 Options (Weekly Expiry)     │
                    └───────────────────┬────────────────────┘
                                        │
        ┌───────────────────┬───────────┴───────────┬───────────────────┐
        ▼                   ▼                       ▼                   ▼
┌──────────────┐    ┌──────────────┐        ┌──────────────┐    ┌──────────────┐
│   BRANCH 1   │    │   BRANCH 2   │        │   BRANCH 3   │    │   BRANCH 4   │
│ CE Leg Entry │    │ PE Leg Entry │        │  P&L Guard   │    │  Time Exit   │
├──────────────┤    ├──────────────┤        ├──────────────┤    ├──────────────┤
│• Sell ATM CE │    │• Sell ATM PE │        │• Checks 10s  │    │• 3:25 PM IST │
│  (10 lots)   │    │  (10 lots)   │        │• Open Pos > 0│    │  Mandatory   │
│• 20% StopLoss│    │• 20% StopLoss│        │• Total P&L:  │    │  Exit        │
│• Auto Square-│    │• Auto Square-│        │  ≥ +₹15,000  │    │• Stop auto-  │
│  off ALL on  │    │  off ALL on  │        │  OR          │    │  mation &    │
│  SL hit      │    │  SL hit      │        │  ≤ −₹15,000  │    │  Square-off  │
│• 15 min wait │    │• 15 min wait │        │• Stop auto-  │    │  all open    │
│• Re-enter 1  │    │• Re-enter 1  │        │  mation &    │    │  positions   │
│  lot CE & PE │    │  lot PE & CE │        │  Square-off  │    │              │
│  (No SL/Tgt) │    │  (No SL/Tgt) │        │  all         │    │              │
└──────────────┘    └──────────────┘        └──────────────┘    └──────────────┘
```

---

## Key Points & Rule Nuances
1. **Auto Square-Off on SL**: When the 20% stop-loss hits on one leg (e.g., CE), it squares off **all open positions** opened by this automation immediately (including the PE leg, even if it is profitable).
2. **Re-entry Wait & Straddle Size**: 15 minutes after an SL trigger, the strategy automatically re-enters at the **current ATM strike** with a smaller 1-lot straddle (1 lot CE + 1 lot PE).
3. **Dual Re-entries on High Volatility**: If both original legs touch their 20% stop-loss during rapid intraday swings, each stop-loss branch fires, creating two sets of 1-lot re-entries.
4. **Unprotected Re-entries Protected by Guards**: The re-entry straddles have no individual stop-loss or profit target. They run until the ±₹15,000 portfolio P&L guard triggers or the 3:25 PM time exit squares them off.
5. **Fixed Portfolio P&L Guard**: The ±₹15,000 bounds are fixed rupee thresholds that protect the total cumulative daily P&L.

---

## 1-Year Backtest Performance (NIFTY 50)

| Metric | Result |
| :--- | :--- |
| **Backtest Period** | 1 Year (246 Trading Sessions) |
| **Initial Capital** | ₹1,00,000 (standard margin) |
| **Slippage** | 0.0% (customizable in UI/CLI) |
| **Win Rate** | **41.06%** (101 Wins / 145 Losses) |
| **Average Win** | **₹1,127.00** |
| **Average Loss** | **₹2,626.00** |
| **Branch 1 (CE SL Hit)** | **116 times (47.2%)** |
| **Branch 2 (PE SL Hit)** | **112 times (45.5%)** |
| **15m Re-entries Executed** | **222 sessions** |
| **Branch 3 Guard Stops (−₹15k)** | **5 sessions** (Hard stop loss saved capital from runaway drawdowns) |
| **Branch 4 (3:25 PM Time Exits)**| **240 sessions** |

---

## Running the Web Backtest Studio
The interactive Web Dashboard is live on:
👉 **`http://127.0.0.1:5001`**

### Features:
- **Interactive Multi-Branch Visual Architecture Graph**: Real-time parallel flowchart.
- **Backtest Configuration Modal**: Matches your UI layout with Time Period dropdown, exclusion toggle, Initial Capital, Slippage %, and remaining test badges.
- **Plotly Interactive Charts**: Cumulative Equity Curve with High Water Mark & Underwater Drawdowns.
- **Monthly Return Heatmap Matrix**: Year-by-month return grid.
- **Daily P&L Distribution**: Histogram of session returns.
- **Session-by-Session Trade Log**: Searchable, filterable by outcome (Wins, Losses, SL Hit, Guard Hit).
- **Session Intraday Audit Modal**: 5-minute intraday P&L trajectory curve, individual leg execution table, and chronological timestamped event log.
- **One-Click CSV Export**: Downloadable full trading log with charges and net P&L.

---

## Running via CLI
You can also run the strategy directly in the terminal:
```bash
# 1-year backtest
.venv/bin/python backtest_cli.py --period "1 year" --capital 100000 --slippage 0.0

# 6-month backtest excluding last 30 days
.venv/bin/python backtest_cli.py --period "6 months" --exclude-last-30
```
