"""
EMA Dashboard: a small local page showing EMAs and basic details for NIFTY 50, BANK NIFTY and GOLDM.

    .venv/bin/python ema_dashboard.py            then open http://127.0.0.1:5050

Data comes from Dhan through engine/dhan_client.py, so it needs the same login as the main app
(cache/broker_config.json or the DHAN_* environment variables) and the Dhan Data API plan.
It only reads market data. It never places orders.
"""

from __future__ import annotations

import csv
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request

from engine.dhan_client import CACHE_DIR, DhanError, get_client, goldm_contracts, scrip_master

app = Flask(__name__)

EMA_LENGTHS = (9, 20, 50, 200)
CHART_BARS = 120
CACHE_SECONDS = 20

# Each instrument: what to chart, and which futures contract supplies volume/OI (indices have no volume).
INSTRUMENTS = {
    "NIFTY": {"name": "NIFTY 50", "seg": "IDX_I", "inst": "INDEX", "id": 13, "fut_prefix": "NIFTY-"},
    "BANKNIFTY": {"name": "BANK NIFTY", "seg": "IDX_I", "inst": "INDEX", "id": 25, "fut_prefix": "BANKNIFTY-"},
    "GOLDM": {"name": "GOLD MINI (MCX)", "seg": "MCX_COMM", "inst": "FUTCOM", "id": None, "fut_prefix": None},
}
TIMEFRAMES = {"5": 5, "15": 15, "60": 60, "D": None}
HISTORY_DAYS = {5: 14, 15: 30, 60: 90}  # enough candles for a settled EMA 200
DAILY_HISTORY_DAYS = 1100

_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_lock = threading.Lock()


# ---------------- contracts ----------------

def _index_future(prefix: str) -> Optional[Tuple[int, str, date]]:
    """Nearest listed index future (security id, symbol, expiry) from Dhan's scrip master CSV."""
    scrip_master()  # downloads today's file into cache/ if needed
    path = os.path.join(CACHE_DIR, f"dhan_scrip_{date.today().isoformat()}.csv")
    best = None
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("SEM_INSTRUMENT_NAME") != "FUTIDX" or r.get("SEM_EXM_EXCH_ID") != "NSE":
                continue
            sym = r.get("SEM_TRADING_SYMBOL", "")
            if not sym.startswith(prefix):
                continue
            try:
                exp = datetime.strptime(r["SEM_EXPIRY_DATE"][:10], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if exp >= date.today() and (best is None or exp < best[2]):
                best = (int(r["SEM_SMST_SECURITY_ID"]), sym, exp)
    return best


def _contract(key: str) -> Dict[str, Any]:
    spec = dict(INSTRUMENTS[key])
    if key == "GOLDM":
        live = [c for c in goldm_contracts() if c[0] >= date.today()]
        if not live:
            raise DhanError("No live GOLDM contract found in Dhan's instrument list.")
        exp, sid, sym = live[0]
        spec.update(id=sid, symbol=sym, expiry=exp.isoformat(), fut={"id": sid, "seg": "MCX_COMM", "inst": "FUTCOM",
                                                                    "symbol": sym})
    else:
        fut = _index_future(spec["fut_prefix"])
        spec["symbol"] = spec["name"]
        spec["fut"] = {"id": fut[0], "seg": "NSE_FNO", "inst": "FUTIDX", "symbol": fut[1]} if fut else None
    return spec


# ---------------- indicators ----------------

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> float:
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] else np.inf
    return float(100 - 100 / (1 + rs))


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    prev = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def _num(x: Any, nd: int = 2) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, nd)


def _last_cross(fast: pd.Series, slow: pd.Series) -> Optional[Dict[str, Any]]:
    diff = np.sign(fast - slow)
    flips = diff.ne(diff.shift()) & diff.shift().notna() & diff.ne(0)
    if not flips.any():
        return None
    when = flips[flips].index[-1]
    return {"direction": "above" if diff.loc[when] > 0 else "below",
            "bars_ago": int(len(diff) - 1 - diff.index.get_loc(when)), "time": when.isoformat()}


# ---------------- data ----------------

def _quote(client, seg: str, sid: int) -> Dict[str, Any]:
    raw = client.request("POST", "/marketfeed/ohlc", {seg: [sid]}, kind="quote")
    row = ((raw or {}).get("data") or {}).get(seg, {}).get(str(sid)) or {}
    o = row.get("ohlc") or {}
    return {"ltp": _num(row.get("last_price")), "open": _num(o.get("open")), "high": _num(o.get("high")),
            "low": _num(o.get("low")), "prev_close": _num(o.get("close"))}


def _candles(client, spec: Dict[str, Any], minutes: Optional[int], now: datetime, oi: bool = False) -> pd.DataFrame:
    if minutes is None:
        df = client.daily(spec["id"], spec["seg"], spec["inst"], now.date() - timedelta(days=DAILY_HISTORY_DAYS), now.date())
        return df
    start = (now - timedelta(days=HISTORY_DAYS[minutes])).replace(hour=0, minute=0, second=0, microsecond=0)
    return client.intraday(spec["id"], spec["seg"], spec["inst"], start, now, minutes, oi=oi)


def build(key: str, tf: str) -> Dict[str, Any]:
    client = get_client()
    now = datetime.now()
    spec = _contract(key)
    minutes = TIMEFRAMES[tf]
    q = _quote(client, spec["seg"], spec["id"])

    df = _candles(client, spec, minutes, now)
    if df.empty:
        raise DhanError("Dhan returned no candles for this instrument and timeframe.")
    df = df.copy()
    if minutes is None and q["ltp"] and q["open"]:
        today = pd.Timestamp(now.date())
        if df.index.tz is not None:
            today = today.tz_localize(df.index.tz)
        bar = {"open": q["open"], "high": q["high"], "low": q["low"], "close": q["ltp"]}
        for k, v in bar.items():
            df.loc[today, k] = v
        df = df.sort_index()

    close = df.close.astype(float)
    price = q["ltp"] or float(close.iloc[-1])
    emas = []
    lines = {}
    for n in EMA_LENGTHS:
        e = _ema(close, n)
        lines[n] = e
        v = float(e.iloc[-1])
        slope = float(e.iloc[-1] - e.iloc[-6]) if len(e) > 6 else 0.0
        emas.append({"length": n, "value": _num(v), "distance": _num(price - v), "pct": _num((price / v - 1) * 100),
                     "slope": _num(slope), "above": price >= v, "settled": len(close) >= int(n * 2.5)})

    above = sum(1 for e in emas if e["above"])
    vals = [e["value"] for e in emas]
    stacked_up = all(vals[i] > vals[i + 1] for i in range(3))
    stacked_down = all(vals[i] < vals[i + 1] for i in range(3))
    if above == 4 and stacked_up:
        verdict, tone = "Strong uptrend: price above all four EMAs, and the EMAs are in order (9 > 20 > 50 > 200).", "up"
    elif above == 0 and stacked_down:
        verdict, tone = "Strong downtrend: price below all four EMAs, and the EMAs are in order (9 < 20 < 50 < 200).", "down"
    elif above == 4:
        verdict, tone = "Up: price is above all four EMAs, but the EMAs are not in order yet (a slower one is on top).", "up"
    elif above == 0:
        verdict, tone = "Down: price is below all four EMAs, but the EMAs are not in order yet.", "down"
    elif above == 3:
        verdict, tone = f"Mostly up: price is above {above} of 4 EMAs.", "up"
    elif above == 1:
        verdict, tone = "Mostly down: price is above only 1 of 4 EMAs.", "down"
    else:
        verdict, tone = "Mixed: price is between the EMAs. No clear direction on this timeframe.", "flat"

    details: Dict[str, Any] = {
        "rsi": _num(_rsi(close), 1), "atr": _num(_atr(df), 1),
        "cross_9_20": _last_cross(lines[9], lines[20]), "cross_50_200": _last_cross(lines[50], lines[200]),
        "vwap": None, "fut_price": None, "fut_symbol": None, "oi": None, "oi_change_pct": None,
    }

    fut = spec.get("fut")
    if fut:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=5)
        f5 = client.intraday(fut["id"], fut["seg"], fut["inst"], start, now, 5, oi=True)
        if not f5.empty:
            day = f5[f5.index >= f5.index[-1].normalize()]
            tp = (day.high + day.low + day.close) / 3
            vol = day.volume if "volume" in day else pd.Series(0, index=day.index)
            if vol.sum() > 0:
                details["vwap"] = _num((tp * vol).sum() / vol.sum())
            details["fut_price"] = _num(day.close.iloc[-1])
            details["fut_symbol"] = fut["symbol"]
            if "open_interest" in f5:
                prev_day = f5[f5.index < day.index[0]]
                oi_now = float(day.open_interest.iloc[-1])
                details["oi"] = int(oi_now)
                if not prev_day.empty and prev_day.open_interest.iloc[-1]:
                    details["oi_change_pct"] = _num((oi_now / float(prev_day.open_interest.iloc[-1]) - 1) * 100)

    tail = df.iloc[-CHART_BARS:]
    chart = {
        "t": [i.isoformat() for i in tail.index],
        "o": [_num(x) for x in tail.open], "h": [_num(x) for x in tail.high],
        "l": [_num(x) for x in tail.low], "c": [_num(x) for x in tail.close],
        "ema": {str(n): [_num(x) for x in lines[n].iloc[-CHART_BARS:]] for n in EMA_LENGTHS},
    }
    prev_close = q["prev_close"]
    return {
        "key": key, "name": spec["name"], "symbol": spec.get("symbol"), "timeframe": tf,
        "updated": now.strftime("%H:%M:%S"), "price": _num(price),
        "change": _num(price - prev_close) if prev_close else None,
        "change_pct": _num((price / prev_close - 1) * 100) if prev_close else None,
        "day": {"open": q["open"], "high": q["high"], "low": q["low"], "prev_close": prev_close},
        "emas": emas, "verdict": verdict, "tone": tone, "details": details, "chart": chart,
        "candles": len(close),
    }


# ---------------- routes ----------------

@app.route("/")
def index():
    return render_template("ema_dashboard.html", instruments={k: v["name"] for k, v in INSTRUMENTS.items()})


@app.route("/api/ema")
def api_ema():
    key = request.args.get("symbol", "NIFTY").upper()
    tf = request.args.get("tf", "5").upper()
    if key not in INSTRUMENTS or tf not in TIMEFRAMES:
        return jsonify({"error": "Choose NIFTY, BANKNIFTY or GOLDM, and a timeframe of 5, 15, 60 or D."}), 400
    with _lock:
        hit = _cache.get((key, tf))
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return jsonify(hit[1])
    try:
        data = build(key, tf)
    except DhanError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:  # keep the page alive and show what went wrong
        app.logger.exception("EMA build failed")
        return jsonify({"error": f"Could not load data: {exc}"}), 500
    with _lock:
        _cache[(key, tf)] = (time.time(), data)
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("EMA_DASHBOARD_PORT", "5050"))
    print(f"EMA Dashboard: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
