"""
Signal scanner: watches all 6 strategies during market hours and sends Telegram alerts.

No orders are placed. For every strategy it alerts when the entry condition hits
(what to buy/sell, estimated price, stop-loss, target), then follows that virtual trade
and alerts on stop-loss / target / trailing / guard / time exits. A summary of all six
strategies goes out after the close.

Market data: NIFTY 5-minute candles and India VIX from Yahoo Finance (free, may lag a
little). Option premiums are Black-Scholes estimates using India VIX as volatility, so
the real price in your broker app will differ: recompute SL/target from your actual fill.

Entry rules follow the backtest code in strategy_registry.py / straddle_engine.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from engine import telegram_notifier as tg
from engine.expiry import nifty_weekly_expiry, trading_days_until
from engine.straddle_engine import bs_call_price, bs_put_price, get_atm_strike
from engine.strategy_registry import get_strategy

logger = logging.getLogger("SignalScanner")

ROOT = os.path.dirname(os.path.dirname(__file__))
STATE_FILE = os.path.join(ROOT, "cache", "signal_scanner_state.json")
HISTORY_FILE = os.path.join(ROOT, "cache", "signal_pnl_history.json")
BROKER_CONFIG_FILE = os.path.join(ROOT, "cache", "broker_config.json")

IST = ZoneInfo("Asia/Kolkata")
LOT_SIZE = 65
WING_PTS = 300
TICK_SECONDS = 30
MISSED_AFTER = timedelta(minutes=15)  # scanner started too late to act on a signal
STALE_AFTER = timedelta(minutes=12)   # newest candle older than this → data warning

ALL_STRATEGIES = ["hedged_selling", "nifty_10_12_put", "nifty_gap_drive", "nifty_1pm_drop", "option_buying", "short_straddle"]

SHORT_NAMES = {
    "hedged_selling": "Iron Butterfly",
    "nifty_10_12_put": "10:12 Put",
    "nifty_gap_drive": "Gap Drive",
    "nifty_1pm_drop": "1 PM Drop",
    "option_buying": "Long Straddle",
    "short_straddle": "Short Straddle",
}

# Single-option buyers: (stop-loss %, target %, trailing % below the highest premium or None, exit time)
BUYER_RULES = {
    "nifty_10_12_put": (0.30, 0.60, 0.15, dtime(15, 20)),
    "nifty_gap_drive": (0.25, 0.50, None, dtime(15, 15)),
    "nifty_1pm_drop": (0.30, 0.60, 0.15, dtime(15, 15)),
}
STRADDLE_EXIT = dtime(15, 25)
SELL_SL_PCT = 0.20
REENTRY_SL_PCT = 0.40
REENTRY_WAIT = timedelta(minutes=15)


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _fmt_time(t: dtime) -> str:
    return datetime.combine(date.today(), t).strftime("%-I:%M %p")


def _rupees(v: float) -> str:
    return f"{'−' if v < 0 else '+'}₹{abs(v):,.0f}"


def fetch_market_data(now: datetime) -> Tuple[pd.DataFrame, float, float]:
    """Today's NIFTY 5-min candles (naive IST index), previous close, and India VIX."""
    import yfinance as yf

    intraday = yf.Ticker("^NSEI").history(period="5d", interval="5m")
    if intraday.empty:
        raise RuntimeError("No NIFTY candles from Yahoo Finance.")
    intraday.index = intraday.index.tz_convert(IST).tz_localize(None)
    intraday = intraday.rename(columns=str.lower)[["open", "high", "low", "close"]]
    today = intraday[intraday.index.date == now.date()]
    earlier = intraday[intraday.index.date < now.date()]
    prev_close = float(earlier["close"].iloc[-1]) if not earlier.empty else 0.0
    vix = 0.0
    try:
        v = yf.Ticker("^INDIAVIX").history(period="5d", interval="1d")
        if not v.empty:
            vix = float(v["Close"].iloc[-1])
    except Exception as exc:
        logger.debug("VIX fetch failed: %s", exc)
    return today, prev_close, vix


class SignalScanner:
    def __init__(self, data_source: Callable[[datetime], Tuple[pd.DataFrame, float, float]] = fetch_market_data,
                 sender: Optional[Callable[[str], None]] = None, state_file: str = STATE_FILE,
                 history_file: str = HISTORY_FILE, config_loader: Optional[Callable[[], Dict[str, Any]]] = None):
        self.data_source = data_source
        self.sender = sender  # tests inject a collector; None → Telegram from broker_config
        self.state_file = state_file
        self.history_file = history_file
        self.config_loader = config_loader  # GitHub runner supplies settings from signals_config.json
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.last_error = ""
        self.last_tick = ""
        self.state: Dict[str, Any] = self._load_state()

    # ---------- config / persistence / alerts ----------

    def _config(self) -> Dict[str, Any]:
        if self.config_loader:
            return self.config_loader()
        try:
            with open(BROKER_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def settings(self) -> Dict[str, Any]:
        s = self._config().get("signals") or {}
        return {
            "enabled": bool(s.get("enabled", True)),
            "strategies": [x for x in s.get("strategies", ALL_STRATEGIES) if x in ALL_STRATEGIES],
        }

    def _capital(self) -> float:
        cfg = self._config()
        return float(cfg.get("allocated_capital") or cfg.get("demo_balance") or 30000.0)

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, self.state_file)

    def _send(self, text: str):
        if self.sender:
            self.sender(text)
            return
        t = self._config().get("telegram") or {}
        if t.get("enabled") and t.get("bot_token") and t.get("chat_id"):
            tg.send_async(t["bot_token"], str(t["chat_id"]), text)

    # ---------- pricing ----------

    def _premium(self, now: datetime, spot: float, strike: int, opt_type: str, iv: float) -> float:
        expiry = nifty_weekly_expiry(now.date())
        mins_left = max(0, (15 * 60 + 30) - (now.hour * 60 + now.minute))
        t = max(0.0008, (max(0.04, mins_left / 375.0) + trading_days_until(now.date(), expiry)) / 252.0)
        fn = bs_call_price if opt_type == "CE" else bs_put_price
        return round(max(0.5, fn(spot, strike, t, 0.065, iv)), 2)

    @staticmethod
    def _symbol(now: datetime, strike: int, opt_type: str) -> str:
        expiry = nifty_weekly_expiry(now.date())
        return f"NIFTY {expiry.strftime('%d %b').upper()} {strike} {opt_type}"

    def _leg(self, now, spot, iv, strike, opt_type, side, sl_pct=None) -> Dict[str, Any]:
        px = self._premium(now, spot, strike, opt_type, iv)
        sl = None
        if sl_pct:
            sl = round(px * (1 + sl_pct) if side == "SELL" else px * (1 - sl_pct), 2)
        return {"symbol": self._symbol(now, strike, opt_type), "strike": strike, "opt": opt_type, "side": side,
                "qty": LOT_SIZE, "entry": px, "cur": px, "high": px, "sl": sl}

    # ---------- main loop ----------

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="SignalScanner")
        self.thread.start()

    def _run(self):
        logger.info("Signal scanner started.")
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning("Signal scanner tick failed: %s", exc)
            self.stop_event.wait(TICK_SECONDS)

    def tick(self, now: Optional[datetime] = None):
        now = now or now_ist()
        settings = self.settings()
        if not settings["enabled"] or now.weekday() >= 5:
            return
        if not (dtime(9, 16) <= now.time() <= dtime(15, 40)):
            return
        candles, prev_close, vix = self.data_source(now)
        self.last_tick = now.strftime("%H:%M:%S")
        self.step(now, candles, prev_close, vix, settings["strategies"])

    def _new_day(self, now: datetime, strategies: List[str]):
        self.state = {
            "date": now.date().isoformat(),
            "announced": False,
            "summary_sent": False,
            "stale_warned": False,
            "strategies": {sid: {"status": "WAITING", "note": "", "trade": None, "pnl": 0.0, "events": []} for sid in strategies},
        }

    def step(self, now: datetime, candles: pd.DataFrame, prev_close: float, vix: float, strategies: Optional[List[str]] = None):
        """One scan at `now` with candles up to `now`. Separate from tick() so tests can replay a day."""
        strategies = strategies or ALL_STRATEGIES
        if self.state.get("date") != now.date().isoformat():
            self._new_day(now, strategies)
        for sid in strategies:  # strategies enabled mid-day
            self.state["strategies"].setdefault(sid, {"status": "WAITING", "note": "", "trade": None, "pnl": 0.0, "events": []})

        # Only candles that have finished (a 5-min bar labelled 10:05 closes at 10:10).
        done = candles[candles.index + timedelta(minutes=5) <= now] if not candles.empty else candles
        if candles.empty or candles.index[-1].date() != now.date():
            if now.time() >= dtime(9, 45) and not self.state.get("no_data_noted"):
                self.state["no_data_noted"] = True
                logger.info("No NIFTY candles for today (holiday or data feed down).")
                self._save_state()
            return

        spot = float(candles["close"].iloc[-1])
        iv = (vix or 14.0) / 100.0
        if now - candles.index[-1] > STALE_AFTER and now.time() < dtime(15, 30) and not self.state["stale_warned"]:
            self.state["stale_warned"] = True
            self._send(f"⚠️ <b>Signal scanner: market data is late</b>\nNewest NIFTY candle is {candles.index[-1].strftime('%H:%M')}. Alerts may be delayed.")

        if not self.state["announced"] and now.time() < dtime(15, 30):
            self.state["announced"] = True
            names = ", ".join(SHORT_NAMES[s] for s in strategies)
            self._send(f"👀 <b>Signal scanner on</b> for {now.strftime('%a %d %b')}\nWatching: {names}\n"
                       f"NIFTY {spot:,.2f} • VIX {vix or 14:.1f}\nNo orders are placed: alerts only.")

        for sid in strategies:
            st = self.state["strategies"][sid]
            if st["status"] == "WAITING":
                self._check_entry(sid, st, now, done, spot, prev_close, iv)
            elif st["status"] == "REENTRY_WAIT" and now >= datetime.fromisoformat(st["reentry_at"]):
                self._enter(sid, st, now, spot, iv, f"Re-entry {int(REENTRY_WAIT.total_seconds() // 60)} min after stop-loss", reentry=True)
            if st["status"] == "OPEN":
                self._manage(sid, st, now, spot, iv)

        if now.time() >= dtime(15, 30) and not self.state["summary_sent"]:
            self.state["summary_sent"] = True
            self._record_day(now)
            self._send(self._summary(now, spot))
        self._save_state()

    # ---------- entries ----------

    def _skip(self, st, note):
        st["status"], st["note"] = "SKIPPED", note

    def _too_late(self, now: datetime, at: dtime) -> bool:
        return now > datetime.combine(now.date(), at) + MISSED_AFTER

    def _check_entry(self, sid, st, now, done, spot, prev_close, iv):
        t = now.time()
        if sid in ("hedged_selling", "option_buying", "short_straddle"):
            if t < dtime(10, 0):
                return
            if self._too_late(now, dtime(10, 0)):
                return self._skip(st, "Missed: scanner started after 10:15")
            return self._enter(sid, st, now, spot, iv, "10:00 AM entry (trades every day)")

        if sid == "nifty_gap_drive":
            if t < dtime(9, 45):
                return
            if self._too_late(now, dtime(9, 45)):
                return self._skip(st, "Missed: scanner started after 10:00")
            if done.empty or not prev_close:
                return self._skip(st, "No previous close / opening candle")
            gap = float(done["open"].iloc[0]) - prev_close
            if abs(gap) < 20:
                return self._skip(st, f"No trade: gap only {gap:+.0f} pts (needs ±20)")
            opt = "CE" if gap > 0 else "PE"
            return self._enter(sid, st, now, spot, iv, f"Gap {'up' if gap > 0 else 'down'} {gap:+.0f} pts at open", opt=opt)

        if sid == "nifty_10_12_put":
            if t < dtime(10, 10):
                return
            if self._too_late(now, dtime(10, 10)):
                return self._skip(st, "Missed: scanner started after 10:25")
            win = done[(done.index.time >= dtime(9, 15)) & (done.index.time <= dtime(10, 5))]
            sig = done[(done.index.time >= dtime(10, 5)) & (done.index.time < dtime(10, 10))]
            if len(win) < 5 or sig.empty:
                return self._skip(st, "Not enough morning candles")
            mid = (win["high"].max() + win["low"].min()) / 2
            close, open_ = float(sig["close"].iloc[-1]), float(sig["open"].iloc[0])
            bearish, below = close < open_, close < mid
            if not (bearish or below):
                return self._skip(st, f"No trade: 10:05 candle green and above range mid {mid:,.0f}")
            why = " and ".join(filter(None, ["10:05 candle red" if bearish else "", f"below range mid {mid:,.0f}" if below else ""]))
            return self._enter(sid, st, now, spot, iv, f"Weak morning: {why}", opt="PE")

        if sid == "nifty_1pm_drop":
            if t < dtime(13, 20):
                return
            if self._too_late(now, dtime(13, 20)):
                return self._skip(st, "Missed: scanner started after 1:35 PM")
            win = done[(done.index.time >= dtime(12, 45)) & (done.index.time <= dtime(13, 15))]
            if len(win) < 4:
                return self._skip(st, "Not enough 12:45–1:15 candles")
            red = int((win["close"] < win["open"]).sum())
            if red < 2:
                return self._skip(st, f"No trade: only {red} red candle(s) 12:45–1:15")
            return self._enter(sid, st, now, spot, iv, f"{red} of {len(win)} candles red 12:45–1:15 PM", opt="PE")

    def _enter(self, sid, st, now, spot, iv, why, opt=None, reentry=False):
        atm = get_atm_strike(spot)
        cap = self._capital()
        if sid in BUYER_RULES:
            sl_pct, tgt_pct, trail, exit_t = BUYER_RULES[sid]
            leg = self._leg(now, spot, iv, atm, opt, "BUY", sl_pct)
            leg["target"] = round(leg["entry"] * (1 + tgt_pct), 2)
            trade = {"kind": "buyer", "legs": [leg], "trail": trail, "exit": exit_t.strftime("%H:%M")}
        elif sid == "option_buying":
            g = round(min(cap, 100000) * 0.10) if cap <= 100000 else 15000
            trade = {"kind": "guard", "legs": [self._leg(now, spot, iv, atm, "CE", "BUY"), self._leg(now, spot, iv, atm, "PE", "BUY")],
                     "target": g, "stop": -g, "exit": STRADDLE_EXIT.strftime("%H:%M")}
        else:
            sl_pct = REENTRY_SL_PCT if reentry else SELL_SL_PCT
            legs = []
            if sid == "hedged_selling":
                legs += [self._leg(now, spot, iv, atm + WING_PTS, "CE", "BUY"), self._leg(now, spot, iv, atm - WING_PTS, "PE", "BUY")]
                g = round(min(cap, 100000) * 0.10) if cap <= 100000 else 15000
            else:
                g = 1500  # ±₹15,000 guard on the default 10 lots, scaled to the 1 lot alerted here
            legs += [self._leg(now, spot, iv, atm, "CE", "SELL", sl_pct), self._leg(now, spot, iv, atm, "PE", "SELL", sl_pct)]
            trade = {"kind": "seller", "legs": legs, "target": g, "stop": -g, "exit": STRADDLE_EXIT.strftime("%H:%M"), "reentry": reentry}
        trade.update({"why": why, "entry_time": now.strftime("%H:%M"), "spot": spot})
        st.update({"status": "OPEN", "trade": trade, "note": why})
        st["events"].append(f"{now.strftime('%H:%M')} {'Re-entry' if reentry else 'Entry'} @ NIFTY {spot:,.0f}")
        self._send(self._entry_text(sid, trade, now, iv, reentry))

    # ---------- exits ----------

    def _trade_pnl(self, trade) -> float:
        return round(sum(((l["cur"] - l["entry"]) if l["side"] == "BUY" else (l["entry"] - l["cur"])) * l["qty"] for l in trade["legs"]), 2)

    def _manage(self, sid, st, now, spot, iv):
        trade = st["trade"]
        for l in trade["legs"]:
            l["cur"] = self._premium(now, spot, l["strike"], l["opt"], iv)
            l["high"] = max(l.get("high", l["cur"]), l["cur"])
        pnl = self._trade_pnl(trade)
        reason = None
        exit_at = datetime.combine(now.date(), dtime.fromisoformat(trade["exit"]))

        if trade["kind"] == "buyer":
            l = trade["legs"][0]
            stop = l["sl"]
            if trade.get("trail"):
                stop = max(stop, round(l["high"] * (1 - trade["trail"]), 2))
            if l["cur"] >= l["target"]:
                reason = f"🎯 Target hit (premium ₹{l['cur']:,.2f} ≥ ₹{l['target']:,.2f})"
            elif l["cur"] <= stop:
                kind = "Trailing stop" if stop > l["sl"] else "Stop-loss"
                reason = f"🛑 {kind} hit (premium ₹{l['cur']:,.2f} ≤ ₹{stop:,.2f})"
        else:
            hit = next((l for l in trade["legs"] if l.get("sl") and l["side"] == "SELL" and l["cur"] >= l["sl"]), None)
            if hit:
                reason = f"🛑 Stop-loss hit on SELL {hit['symbol']} (₹{hit['cur']:,.2f} ≥ ₹{hit['sl']:,.2f})"
            elif pnl >= trade["target"]:
                reason = f"🎯 Profit guard hit ({_rupees(pnl)} ≥ +₹{trade['target']:,.0f})"
            elif pnl <= trade["stop"]:
                reason = f"🧯 Loss guard hit ({_rupees(pnl)} ≤ −₹{abs(trade['stop']):,.0f})"
        if not reason and now >= exit_at:
            reason = f"🕞 Time exit ({exit_at.strftime('%-I:%M %p')})"
        if not reason:
            return

        st["pnl"] = round(st["pnl"] + pnl, 2)
        st["events"].append(f"{now.strftime('%H:%M')} Exit {_rupees(pnl)}")
        # Sellers re-enter once 15 min after a leg stop-loss (re-entry legs carry a 40% SL), like the backtest.
        if trade["kind"] == "seller" and reason.startswith("🛑") and not trade.get("reentry") and now + REENTRY_WAIT < exit_at:
            st["status"], st["reentry_at"] = "REENTRY_WAIT", (now + REENTRY_WAIT).isoformat()
            next_step = f"Re-entry signal at {(now + REENTRY_WAIT).strftime('%-I:%M %p')}."
        else:
            st["status"] = "CLOSED"
            next_step = "Done for the day."
        st["note"] = reason.split(" ", 1)[1].split(" (")[0]
        self._send(self._exit_text(sid, trade, reason, pnl, st["pnl"], next_step, now))
        st["trade"] = None if st["status"] == "CLOSED" else trade

    # ---------- message text ----------

    def _entry_text(self, sid, trade, now, iv, reentry) -> str:
        name = get_strategy(sid)["name"]
        head = "🔁 RE-ENTRY" if reentry else "🔔 SIGNAL"
        lines = [f"{head}: <b>{tg.esc(name)}</b>", f"Why: {tg.esc(trade['why'])}", ""]
        legs = sorted(trade["legs"], key=lambda l: l["side"] != "BUY")  # hedges (buys) first
        for i, l in enumerate(legs, 1):
            icon = "🟢" if l["side"] == "BUY" else "🔴"
            lines.append(f"{i}. {icon} <b>{l['side']}</b> <code>{tg.esc(l['symbol'])}</code> × {l['qty']} @ ~₹{l['entry']:,.2f}")
            if l["side"] == "SELL" and l.get("sl"):
                lines.append(f"    SL: buy back if ≥ ₹{l['sl']:,.2f}")
            elif l.get("target"):
                trail = f" • then trail {int(trade['trail'] * 100)}% below the high" if trade.get("trail") else ""
                lines.append(f"    SL ₹{l['sl']:,.2f} • Target ₹{l['target']:,.2f}{trail}")
        lines.append("")
        if trade["kind"] == "seller":
            lines.append(f"• Any SL hit → close ALL legs. Guard: exit all at +₹{trade['target']:,.0f} / −₹{abs(trade['stop']):,.0f}.")
            lines.append("• Place the BUY hedges before the SELLs." if len(legs) > 2 else "• ⚠️ Naked sell: unlimited risk, ~₹1.6L+ margin per leg.")
        elif trade["kind"] == "guard":
            lines.append(f"• No leg SL. Guard: exit both at +₹{trade['target']:,.0f} / −₹{abs(trade['stop']):,.0f}.")
            cost = sum(l["entry"] * l["qty"] for l in legs)
            lines.append(f"• Cost ≈ ₹{cost:,.0f} (max loss).")
        else:
            l = legs[0]
            lines.append(f"• Cost ≈ ₹{l['entry'] * l['qty']:,.0f} • risk ≈ ₹{(l['entry'] - l['sl']) * l['qty']:,.0f} • reward ≈ ₹{(l['target'] - l['entry']) * l['qty']:,.0f}")
        lines.append(f"• Exit by {datetime.strptime(trade['exit'], '%H:%M').strftime('%-I:%M %p')} if nothing hits.")
        lines.append("")
        lines.append(f"<i>NIFTY {trade['spot']:,.2f} • prices are estimates (VIX {iv * 100:.1f}): check the real premium and recompute SL/target from your fill. {now.strftime('%d %b %H:%M')}</i>")
        return "\n".join(lines)

    def _exit_text(self, sid, trade, reason, pnl, day_pnl, next_step, now) -> str:
        lines = [f"{reason.split(' ', 1)[0]} EXIT: <b>{tg.esc(SHORT_NAMES[sid])}</b>", tg.esc(reason.split(" ", 1)[1]), ""]
        for l in trade["legs"]:
            action = "SELL" if l["side"] == "BUY" else "BUY back"
            lines.append(f"• {action} <code>{tg.esc(l['symbol'])}</code> @ ~₹{l['cur']:,.2f} (entry ₹{l['entry']:,.2f})")
        lines += ["", f"Est. P&L: <b>{_rupees(pnl)}</b>" + (f" • strategy today {_rupees(day_pnl)}" if day_pnl != pnl else ""),
                  next_step, f"<i>{now.strftime('%d %b %H:%M')}</i>"]
        return "\n".join(lines)

    def _summary(self, now, spot) -> str:
        lines = [f"📋 <b>Signal summary {now.strftime('%a %d %b')}</b>  (NIFTY close {spot:,.2f})", ""]
        total = 0.0
        for sid, st in self.state["strategies"].items():
            if st["status"] in ("CLOSED", "OPEN", "REENTRY_WAIT") or st["pnl"]:
                total += st["pnl"]
                lines.append(f"• <b>{SHORT_NAMES[sid]}</b>: {_rupees(st['pnl'])}  <i>{tg.esc(st['note'])}</i>")
            else:
                lines.append(f"• <b>{SHORT_NAMES[sid]}</b>: — <i>{tg.esc(st['note'] or 'No signal')}</i>")
        lines += ["", f"Today, every signal at 1 lot: <b>{_rupees(total)}</b> before charges"]
        t = self.totals(now.date())
        lines.append(f"This week {_rupees(t['week'])} • This month {_rupees(t['month'])} • Since start {_rupees(t['all'])} ({t['days']} days)")
        return "\n".join(lines)

    # ---------- P&L history ----------

    def history(self) -> List[Dict[str, Any]]:
        try:
            with open(self.history_file) as f:
                return json.load(f)
        except Exception:
            return []

    def _record_day(self, now: datetime):
        """Append today's per-strategy result (replacing any earlier record for the same date)."""
        day = {"date": now.date().isoformat(), "strategies": {}}
        for sid, st in self.state["strategies"].items():
            day["strategies"][sid] = {"pnl": round(st["pnl"], 2), "status": st["status"], "note": st["note"]}
        day["total"] = round(sum(v["pnl"] for v in day["strategies"].values()), 2)
        hist = [h for h in self.history() if h["date"] != day["date"]] + [day]
        hist.sort(key=lambda h: h["date"])
        os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        with open(self.history_file, "w") as f:
            json.dump(hist, f, indent=1)

    def totals(self, today: date) -> Dict[str, Any]:
        hist = self.history()
        monday = today - timedelta(days=today.weekday())
        week = sum(h["total"] for h in hist if h["date"] >= monday.isoformat())
        month = sum(h["total"] for h in hist if h["date"][:7] == today.isoformat()[:7])
        per = {sid: round(sum(h["strategies"].get(sid, {}).get("pnl", 0) for h in hist), 2) for sid in ALL_STRATEGIES}
        return {"week": round(week, 2), "month": round(month, 2), "all": round(sum(h["total"] for h in hist), 2),
                "days": len(hist), "per_strategy": per}

    # ---------- UI ----------

    def sample_text(self) -> str:
        """A made-up 10:12 Put signal so the user can check the message format."""
        now = now_ist().replace(hour=10, minute=10)
        spot, iv = 23053.10, 0.153
        leg = self._leg(now, spot, iv, get_atm_strike(spot), "PE", "BUY", 0.30)
        leg["target"] = round(leg["entry"] * 1.60, 2)
        trade = {"kind": "buyer", "legs": [leg], "trail": 0.15, "exit": "15:20", "spot": spot,
                 "why": "SAMPLE ONLY, not a real signal: 10:05 candle red and below range mid 23,070"}
        return "🧪 <b>SAMPLE</b>\n" + self._entry_text("nifty_10_12_put", trade, now, iv, False)

    def get_status(self) -> Dict[str, Any]:
        rows = []
        for sid in ALL_STRATEGIES:
            st = (self.state.get("strategies") or {}).get(sid)
            pnl = st["pnl"] if st else 0.0
            if st and st["status"] == "OPEN" and st.get("trade"):
                pnl += self._trade_pnl(st["trade"])  # include the open trade's running P&L
            rows.append({"id": sid, "name": SHORT_NAMES[sid], "status": st["status"] if st else "—",
                         "note": st["note"] if st else "", "pnl": round(pnl, 2),
                         "open_trade": st.get("trade") if st and st["status"] == "OPEN" else None})
        return {"settings": self.settings(), "date": self.state.get("date"), "last_tick": self.last_tick,
                "last_error": self.last_error, "running": bool(self.thread and self.thread.is_alive()), "strategies": rows}


signal_scanner = SignalScanner()
