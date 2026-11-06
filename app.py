"""
Flask Web Application for NIFTY 50 Short-Premium ATM Straddle Backtester
Provides REST API endpoints for:
- Running dynamic backtests with custom parameters
- Fetching intraday day replays
- Exporting CSV reports
- Serving modern algorithmic trading dashboard
"""

import os
import csv
import io
import json
import warnings
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, Response, send_file
import pandas as pd
import numpy as np

# Suppress pandas deprecation noise
warnings.filterwarnings('ignore')

from engine.data_feed import NiftyDataFeed
from engine.gold_feed import GoldMiniFeed
from engine.straddle_engine import NiftyStraddleEngine
from engine.strategy_registry import list_strategies, get_strategy, UnifiedStrategyRunner
from engine.live_runner import algo_runner
from engine.dhan_broker import DhanBroker
from engine.dhan_client import dhan_credentials
from engine import telegram_notifier as tg
from engine.signal_scanner import signal_scanner, ALL_STRATEGIES

app = Flask(__name__, template_folder="templates", static_folder="static")
# Pick up template edits without restarting the server.
app.config["TEMPLATES_AUTO_RELOAD"] = True

data_feed = NiftyDataFeed()
gold_feed = GoldMiniFeed()

# In-memory store of last backtest results for quick day inspection & export
LATEST_BACKTEST_RESULTS = {}

BROKER_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "cache", "broker_config.json")

DEFAULT_CAPITAL = 30000.0


DHAN_SECRETS = ("access_token", "pin", "totp_secret")


def default_broker_config():
    return {
        "broker": "dhan",
        "account_type": "demo",
        "demo_balance": DEFAULT_CAPITAL,
        "original_balance": DEFAULT_CAPITAL,
        # Original account: "full" uses all available Dhan funds, "custom" uses allocated_capital.
        "allocation_mode": "custom",
        "allocated_capital": DEFAULT_CAPITAL,
        # Telegram alerts when any strategy's entry/exit condition hits (no orders).
        "signals": {
            "enabled": True,
            "strategies": list(ALL_STRATEGIES),
        },
        "telegram": {
            "bot_token": "",
            "chat_id": "",
            "chat_name": "",
            "enabled": False,
        },
        # The only broker and market-data source. Either a pasted access token (24 h) or
        # PIN + TOTP secret for automatic daily login.
        "dhan": {
            "client_id": "",
            "access_token": "",
            "pin": "",
            "totp_secret": "",
            "is_connected": False,
        },
    }


def load_broker_config():
    cfg = default_broker_config()
    if os.path.exists(BROKER_CONFIG_FILE):
        try:
            with open(BROKER_CONFIG_FILE, "r") as f:
                saved = json.load(f)
            # Old broker sections (groww, kite, indstocks) are dropped: Dhan is the only broker.
            cfg.update({k: v for k, v in saved.items()
                        if k not in ("groww", "kite", "indstocks", "dhan", "telegram", "signals", "broker")})
            for section in ("signals", "telegram", "dhan"):
                if isinstance(saved.get(section), dict):
                    cfg[section].update(saved[section])
        except Exception:
            pass
    if not cfg.get("demo_balance"):
        cfg["demo_balance"] = DEFAULT_CAPITAL
    # Real account disabled for now: backtest + Telegram signal alerts only.
    cfg["account_type"] = "demo"
    return cfg


def public_broker_config(cfg):
    """Return config for the UI without leaking secrets."""
    out = json.loads(json.dumps(cfg))
    dhan = out.get("dhan", {})
    dhan["has_access_token"] = bool(dhan.get("access_token"))
    dhan["has_auto_login"] = bool(dhan.get("pin") and dhan.get("totp_secret"))
    # Secrets never leave the server; blank fields on save keep the stored values.
    for secret_key in DHAN_SECRETS:
        dhan[secret_key] = ""
    out["dhan"] = dhan
    out["telegram"] = public_telegram_config(cfg)
    return out

def public_telegram_config(cfg):
    t = dict(cfg.get("telegram") or {})
    t["has_bot_token"] = bool(t.get("bot_token"))
    t["bot_token"] = ""
    return t


def live_broker(cfg):
    """Broker for the Original account's balance (Dhan)."""
    return DhanBroker(**dhan_credentials(cfg))


def save_broker_config(cfg):
    os.makedirs(os.path.dirname(BROKER_CONFIG_FILE), exist_ok=True)
    with open(BROKER_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/broker/config", methods=["GET", "POST"])
def broker_config_endpoint():
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = load_broker_config()
        if (data.get("account_type") not in (None, cfg.get("account_type"))) and algo_runner.is_running:
            return jsonify({"success": False, "error": "Stop the algo before switching accounts."}), 409
        cfg["broker"] = "dhan"
        if data.get("account_type") in ("demo",):  # "original" disabled for now
            cfg["account_type"] = data["account_type"]
        if data.get("demo_balance"):
            cfg["demo_balance"] = float(data["demo_balance"])
        if isinstance(data.get("dhan"), dict):
            incoming = {k: str(v).strip() for k, v in data["dhan"].items() if k in ("client_id",) + DHAN_SECRETS}
            # Keep previously saved secrets if the UI sent blanks.
            for secret_key in DHAN_SECRETS:
                if not incoming.get(secret_key):
                    incoming.pop(secret_key, None)
            cfg["dhan"].update(incoming)
            cfg["dhan"]["last_connected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_broker_config(cfg)
        return jsonify({"success": True, "data": public_broker_config(cfg)})
    else:
        return jsonify({"success": True, "data": public_broker_config(load_broker_config())})

@app.route("/api/broker/test", methods=["POST"])
def broker_test_endpoint():
    data = request.get_json() or {}
    creds = data.get("credentials", {})
    cfg = load_broker_config()
    if data.get("broker", "dhan") != "dhan":
        return jsonify({"success": False, "error": "Only Dhan is supported."}), 400

    d = {"client_id": (creds.get("client_id") or cfg["dhan"].get("client_id") or "").strip()}
    for key in DHAN_SECRETS:
        d[key] = (creds.get(key) or "").strip()
    # A freshly pasted token replaces saved auto-login, and vice versa; blanks keep what is saved.
    if not d["access_token"] and not (d["pin"] and d["totp_secret"]):
        for key in DHAN_SECRETS:
            d[key] = cfg["dhan"].get(key) or ""
    elif d["access_token"] and not (d["pin"] and d["totp_secret"]):
        d["pin"] = d["totp_secret"] = ""
    elif not d["access_token"]:
        d["access_token"] = ""
    if not d["client_id"] or not (d["access_token"] or (d["pin"] and d["totp_secret"])):
        return jsonify({"success": False, "error": "Dhan Client ID plus an access token (or PIN + TOTP secret) are required."}), 400

    started = datetime.now()
    dh = DhanBroker(**d)
    ok, msg = dh.connect()
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    funds = dh.get_balance()
    latency_ms = int((datetime.now() - started).total_seconds() * 1000)
    balance = funds.get("balance") or 0.0
    cfg["broker"] = "dhan"
    cfg["dhan"].update(d)
    cfg["dhan"].update({
        "is_connected": True,
        "token_validity": funds.get("token_validity", ""),
        "data_plan": funds.get("data_plan", ""),
        "last_connected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_broker_config(cfg)
    warn = " ".join(funds.get("warnings") or [])
    return jsonify({
        "success": True,
        "broker": "Dhan",
        "mode": "LIVE",
        "status": "CONNECTED",
        "balance": balance,
        "data_plan": funds.get("data_plan", ""),
        "message": f"{msg} Available funds ₹{balance:,.2f}." + (f" {warn}" if warn else ""),
        "latency_ms": latency_ms,
        "data": public_broker_config(cfg),
    })


@app.route("/api/telegram/config", methods=["GET", "POST"])
def telegram_config_endpoint():
    """Save Telegram alert settings. A blank bot token keeps the saved one."""
    cfg = load_broker_config()
    if request.method == "POST":
        data = request.get_json() or {}
        token = str(data.get("bot_token") or "").strip()
        if token:
            cfg["telegram"]["bot_token"] = token
        if "chat_id" in data:
            cfg["telegram"]["chat_id"] = str(data.get("chat_id") or "").strip()
        if "enabled" in data:
            cfg["telegram"]["enabled"] = bool(data["enabled"])
        save_broker_config(cfg)
    return jsonify({"success": True, "data": public_telegram_config(cfg)})


@app.route("/api/telegram/detect", methods=["POST"])
def telegram_detect_endpoint():
    """Find the chat ID of whoever last messaged the bot, and save it."""
    data = request.get_json() or {}
    cfg = load_broker_config()
    token = str(data.get("bot_token") or "").strip() or cfg["telegram"].get("bot_token", "")
    chat, err = tg.detect_chat_id(token)
    if not chat:
        return jsonify({"success": False, "error": err}), 400
    cfg["telegram"].update({"bot_token": token, "chat_id": chat["chat_id"], "chat_name": chat["name"]})
    save_broker_config(cfg)
    return jsonify({"success": True, "chat": chat, "data": public_telegram_config(cfg)})


@app.route("/api/telegram/test", methods=["POST"])
def telegram_test_endpoint():
    t = load_broker_config()["telegram"]
    ok, err = tg.send_message(
        t.get("bot_token", ""), t.get("chat_id", ""),
        "✅ <b>StraddleCore alerts connected</b>\nYou'll get entries, stop-losses, guard hits, exits, "
        "rejected orders and 'place this trade manually' alerts here.",
    )
    if not ok:
        return jsonify({"success": False, "error": err}), 400
    return jsonify({"success": True})


@app.route("/api/signals/status", methods=["GET"])
def signals_status_endpoint():
    return jsonify({"success": True, "data": signal_scanner.get_status()})


@app.route("/api/signals/config", methods=["POST"])
def signals_config_endpoint():
    data = request.get_json() or {}
    cfg = load_broker_config()
    if "enabled" in data:
        cfg["signals"]["enabled"] = bool(data["enabled"])
    if isinstance(data.get("strategies"), list):
        cfg["signals"]["strategies"] = [s for s in ALL_STRATEGIES if s in data["strategies"]]
    save_broker_config(cfg)
    return jsonify({"success": True, "data": signal_scanner.get_status()})


@app.route("/api/signals/test", methods=["POST"])
def signals_test_endpoint():
    t = load_broker_config()["telegram"]
    ok, err = tg.send_message(t.get("bot_token", ""), t.get("chat_id", ""), signal_scanner.sample_text())
    if not ok:
        return jsonify({"success": False, "error": err}), 400
    return jsonify({"success": True})


@app.route("/api/strategies", methods=["GET"])
def strategies_endpoint():
    return jsonify({"success": True, "data": list_strategies()})


@app.route("/api/broker/balance", methods=["GET"])
def broker_balance_endpoint():
    cfg = load_broker_config()
    account_type = "demo"  # real account disabled for now (was: request.args / cfg account_type)
    if account_type == "demo":
        bal = float(cfg.get("demo_balance", DEFAULT_CAPITAL))
        return jsonify({
            "success": True,
            "account_type": "demo",
            "balance": bal,
            "allocated_capital": bal,
            "label": "Demo virtual cash",
        })
    funds = live_broker(cfg).get_balance()
    live_bal = float(funds.get("balance") or 0.0)
    alloc_mode = cfg.get("allocation_mode", "custom")
    custom_alloc = float(cfg.get("allocated_capital") or DEFAULT_CAPITAL)
    if not funds.get("success"):
        return jsonify({
            "success": False,
            "account_type": "original",
            "balance": live_bal or float(cfg.get("original_balance", DEFAULT_CAPITAL)),
            "allocated_capital": custom_alloc,
            "allocation_mode": alloc_mode,
            "error": funds.get("error") or "Could not fetch Dhan funds.",
        }), 400
    cfg["original_balance"] = live_bal
    save_broker_config(cfg)
    allocated = live_bal if alloc_mode == "full" else min(custom_alloc, live_bal)
    return jsonify({
        "success": True,
        "account_type": "original",
        "balance": live_bal,
        "allocated_capital": round(allocated, 2),
        "allocation_mode": alloc_mode,
        "custom_capital": custom_alloc,
        "broker": "dhan",
        "label": funds.get("profile_name") or "Live funds",
        "ucc": funds.get("ucc") or "",
        "active_segments": funds.get("active_segments") or [],
        "warnings": funds.get("warnings") or [],
    })


@app.route("/api/demo/funds", methods=["POST"])
def demo_funds_endpoint():
    """Manage the demo (paper) account's virtual cash: add, set or reset."""
    data = request.get_json() or {}
    action = data.get("action", "add")
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Amount must be a number."}), 400

    cfg = load_broker_config()
    bal = float(cfg.get("demo_balance", DEFAULT_CAPITAL))
    if action == "reset":
        bal = DEFAULT_CAPITAL
    elif action == "set":
        if amount <= 0:
            return jsonify({"success": False, "error": "Balance must be greater than zero."}), 400
        bal = amount
    elif action == "add":
        if amount <= 0:
            return jsonify({"success": False, "error": "Enter an amount greater than zero."}), 400
        bal += amount
    else:
        return jsonify({"success": False, "error": f"Unknown action: {action}"}), 400

    cfg["demo_balance"] = round(bal, 2)
    save_broker_config(cfg)
    return jsonify({"success": True, "account_type": "demo", "balance": cfg["demo_balance"]})


@app.route("/api/algo/capital", methods=["POST"])
def algo_capital_endpoint():
    """Set how much of the Original (Dhan) account the algo may use: full funds or a custom amount."""
    data = request.get_json() or {}
    mode = data.get("mode", "custom")
    if mode not in ("full", "custom"):
        return jsonify({"success": False, "error": f"Unknown mode: {mode}"}), 400
    if algo_runner.is_running:
        return jsonify({"success": False, "error": "Stop the algo before changing capital."}), 400

    cfg = load_broker_config()
    if mode == "custom":
        try:
            amount = float(data.get("amount") or 0)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Amount must be a number."}), 400
        if amount <= 0:
            return jsonify({"success": False, "error": "Capital must be greater than zero."}), 400
        cfg["allocated_capital"] = round(amount, 2)
    cfg["allocation_mode"] = mode
    save_broker_config(cfg)
    return jsonify({"success": True, "allocation_mode": mode, "allocated_capital": cfg.get("allocated_capital")})


@app.route("/api/algo/square_off_all", methods=["POST"])
def algo_square_off_all_endpoint():
    res = algo_runner.square_off_all_now("Manual UI Square Off")
    return jsonify(res)

@app.route("/api/algo/start", methods=["POST"])
def algo_start_endpoint():
    data = request.get_json() or {}
    data["account_type"] = "demo"  # real account disabled for now
    res = algo_runner.start(config_override=data)
    return jsonify(res)

@app.route("/api/algo/stop", methods=["POST"])
def algo_stop_endpoint():
    res = algo_runner.stop()
    return jsonify(res)

@app.route("/api/algo/status", methods=["GET"])
def algo_status_endpoint():
    return jsonify({"success": True, "data": algo_runner.get_status()})

@app.route("/api/algo/test_cycle", methods=["POST"])
def algo_test_cycle_endpoint():
    data = request.get_json() or {}
    res = algo_runner.execute_instant_paper_cycle(strategy_mode=data.get("strategy_mode"))
    return jsonify(res)

@app.route("/api/backtest", methods=["POST"])
def run_backtest_api():
    global LATEST_BACKTEST_RESULTS
    try:
        params = request.get_json() or {}
        
        period = params.get("period", "1 year")
        strategy_mode = params.get("strategy_mode", "short_straddle")
        capital = float(params.get("capital", 30000.0))
        slippage = float(params.get("slippage", 0.0))
        exclude_last_30 = bool(params.get("exclude_last_30", False))
        execution_mode = params.get("execution_mode", "fyers_calibrated")
        
        raw_initial_lots = params.get("initial_lots")
        initial_lots = int(raw_initial_lots) if raw_initial_lots not in (None, "", "auto", 0) else (10 if strategy_mode == "short_straddle" else None)
        
        raw_reentry_lots = params.get("reentry_lots")
        reentry_lots = int(raw_reentry_lots) if raw_reentry_lots not in (None, "", "auto", 0) else 1
        
        lot_size = int(params.get("lot_size", 65))
        sl_pct = float(params.get("sl_pct", 0.20))
        reentry_wait = int(params.get("reentry_wait_minutes", 15))
        # Stop-loss on re-entry legs; 0 turns it off (old unprotected re-entry).
        reentry_sl_pct = float(params.get("reentry_sl_pct", 0.40) or 0) or None
        
        pl_target = float(params["pl_target"]) if "pl_target" in params and params["pl_target"] not in (None, "") else None
        pl_stop = float(params["pl_stop"]) if "pl_stop" in params and params["pl_stop"] not in (None, "") else None

        # Fetch data
        if strategy_mode == "gold_trend_futures":
            # The lots/lot-size config is shared with the NIFTY strategies; Gold Mini is sized
            # from capital (≈₹1.5L margin per 100 g lot) so it never exceeds the account.
            initial_lots = max(1, int(capital // get_strategy("gold_trend_futures")["capital_per_lot"]))
            lot_size = 100
            hist_data = gold_feed.get_historical_intraday_data(period=period)
        else:
            hist_data = data_feed.get_historical_intraday_data(
                period=period,
                exclude_last_30=exclude_last_30
            )
        
        if not hist_data:
            return jsonify({"success": False, "error": "No historical data available for the selected period."}), 400

        # Run unified multi-strategy engine
        runner = UnifiedStrategyRunner(
            strategy_id=strategy_mode,
            period=period,
            capital=capital,
            initial_lots=initial_lots,
            reentry_lots=reentry_lots,
            lot_size=lot_size,
            sl_pct=sl_pct,
            reentry_wait_minutes=reentry_wait,
            reentry_sl_pct=reentry_sl_pct,
            pl_target=pl_target,
            pl_stop=pl_stop,
            slippage=slippage,
            execution_mode=execution_mode,
        )

        results = runner.run(hist_data)
        results["params"] = {
            "period": period,
            "strategy_mode": strategy_mode,
            "capital": capital,
            "slippage": slippage,
            "exclude_last_30": exclude_last_30,
            "execution_mode": execution_mode,
            "initial_lots": initial_lots or 1,
            "reentry_lots": reentry_lots or 1,
            "lot_size": lot_size,
            "sl_pct": sl_pct,
            "reentry_wait_minutes": reentry_wait,
            "reentry_sl_pct": reentry_sl_pct or 0,
            "pl_target": pl_target or round(capital * 0.10),
            "pl_stop": pl_stop or -round(capital * 0.10),
        }
        
        results["roi_pct"] = round((results["total_net_pnl"] / capital) * 100.0, 2) if capital else 0.0
        results["capital"] = capital

        LATEST_BACKTEST_RESULTS = results

        return jsonify({"success": True, "data": results})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/day_detail/<date_str>", methods=["GET"])
def get_day_detail(date_str):
    global LATEST_BACKTEST_RESULTS
    try:
        if not LATEST_BACKTEST_RESULTS or "trade_logs" not in LATEST_BACKTEST_RESULTS:
            hist_data = data_feed.get_historical_intraday_data(period="1 year", exclude_last_30=True)
            engine = NiftyStraddleEngine()
            LATEST_BACKTEST_RESULTS = engine.run_backtest(hist_data)

        day_log = next((t for t in LATEST_BACKTEST_RESULTS["trade_logs"] if t["date"] == date_str), None)
        if not day_log:
            return jsonify({"success": False, "error": f"Day {date_str} not found in current results."}), 404

        return jsonify({"success": True, "data": day_log})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/export_csv", methods=["GET"])
def export_csv():
    global LATEST_BACKTEST_RESULTS
    if not LATEST_BACKTEST_RESULTS or "trade_logs" not in LATEST_BACKTEST_RESULTS:
        hist_data = data_feed.get_historical_intraday_data(period="1 year", exclude_last_30=True)
        engine = NiftyStraddleEngine()
        LATEST_BACKTEST_RESULTS = engine.run_backtest(hist_data)

    output = io.StringIO()
    writer = csv.writer(output)
    
    # Headers
    writer.writerow([
        "Date", "Day", "Entry Time", "Spot at 10:00 AM", 
        "CE Strike", "CE Entry", "CE Exit", "CE SL Hit",
        "PE Strike", "PE Entry", "PE Exit", "PE SL Hit",
        "Re-entry Count", "Re-entry PnL (Rs)", "Exit Reason",
        "Gross PnL (Rs)", "Charges (Rs)", "Net PnL (Rs)", "Cumulative PnL (Rs)"
    ])

    # Sort descending by date (latest first) to match table UI
    sorted_trades = sorted(LATEST_BACKTEST_RESULTS["trade_logs"], key=lambda x: x["date"], reverse=True)
    for t in sorted_trades:
        writer.writerow([
            t["date"], t["day_name"], t["entry_time"], t["spot_at_entry"],
            t["ce_strike"], t["ce_entry"], t["ce_exit"], "YES" if t["ce_sl_hit"] else "NO",
            t["pe_strike"], t["pe_entry"], t["pe_exit"], "YES" if t["pe_sl_hit"] else "NO",
            t["reentry_count"], t["reentry_pnl"], t["exit_reason"],
            t["gross_pnl"], t["charges"], t["net_pnl"], t["cum_pnl"]
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=nifty_straddle_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
    )

signal_scanner.start()
if not algo_runner.is_running:
    algo_runner.account_type = "demo"  # real account disabled for now; saved state may still say "original"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
