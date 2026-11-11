"""
Iron Butterfly backtest on real NIFTY option prices (Dhan expired-options candles).

    .venv/bin/python scripts/real_premium_backtest.py --from 2026-08-01 --to 2026-11-04

Follows the rules the Telegram alerts give (engine/signal_scanner.py), not the modelled
backtest in the app:
  10:00   buy ATM+300 CE and ATM-300 PE wings, sell the ATM CE and PE (entry = 10:00 candle open)
  SL      a sold leg rising 20% above entry closes the whole set
  +15 min one re-entry with wings, sold legs carry a 40% SL
  guard   day P&L of ±10% of capital (max ₹10,000) closes everything
  15:25   exit at the 15:25 candle open

Stop-losses trigger on the 5-minute candle high and fill at the SL price (or the candle open
if it gapped past it); the other legs close at that candle's close. Charges use the app's
formula. Quantity is 65 per lot throughout, so every day is comparable at today's lot size.

Data: Dhan's /charts/rollingoption (Data API plan), which returns weekly-option candles by
offset from ATM (ATM-10 … ATM+10) with the actual strike on every candle. All offsets are
fetched once for the whole range and regrouped by strike, so each leg keeps its fixed strike
for the day. NIFTY spot comes from Dhan's intraday index candles. Rolling candles mix two strikes
whenever NIFTY crosses an ATM boundary inside the candle; on those candles only the close is used
(checked against live contracts: closes within ~₹2, no overstated highs), so a brief spike inside
such a candle can be missed and a stop-loss may fire a candle late. Results lean slightly optimistic. Responses are cached in
cache/dhan_hist/, so re-runs are fast and work offline. Dhan serves this data a day or two
per request, so the first run of a long range takes a while (≈ 42 requests per 2 trading days). --expiry-code picks the weekly
contract (Dhan: 1 = nearest expiry); the reported expiry is the calendar estimate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.straddle_engine import NiftyStraddleEngine  # noqa: E402  (charges formula)

CACHE = os.path.join(ROOT, "cache", "dhan_hist")
MAX_OFFSET = 10   # Dhan serves ATM-10 … ATM+10 for index options
WING_PTS = 300
SL_PCT, REENTRY_SL_PCT = 0.20, 0.40
REENTRY_WAIT = timedelta(minutes=15)
ENTRY, EXIT = time(10, 0), time(15, 25)
BAR = timedelta(minutes=5)

_charges = NiftyStraddleEngine(strategy_mode="hedged_selling").calculate_charges


# ---------- Dhan data ----------

class DhanHistory:
    def __init__(self, start: date, end: date, expiry_code: int = 1, client=None):
        from engine.dhan_client import get_client

        self.client = client or get_client()
        if not self.client.configured:
            sys.exit("Dhan keys missing: save them in the app, or set DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN "
                     "(or DHAN_PIN + DHAN_TOTP_SECRET).")
        self.start, self.end, self.expiry_code = start, end, expiry_code
        os.makedirs(CACHE, exist_ok=True)
        self._spot = self._cached(f"spot_{start}_{end}", self._fetch_spot)
        rows = []
        for opt in ("CE", "PE"):
            for off in range(-MAX_OFFSET, MAX_OFFSET + 1):
                strike = "ATM" if off == 0 else f"ATM{off:+d}"
                df = self._cached(f"{opt}_{strike}_{expiry_code}_{start}_{end}",
                                  lambda s=strike, o=opt: self.client.rolling_option(s, o, start, end, interval=5,
                                                                                     expiry_code=expiry_code))
                if not df.empty and "strike" in df:
                    rows.append(df.assign(opt=opt, strike=df["strike"].round().astype(int)))
        if not rows:
            sys.exit(f"Dhan returned no option candles: {self.client.last_error or 'check the Data API plan'}")
        allrows = pd.concat(rows)
        # The strike label is the ATM+offset strike at the candle's CLOSE. If NIFTY crossed an ATM boundary
        # inside the candle, its open/high/low mix two strikes, so only the close is trusted there.
        # A candle is clean when NIFTY's 5-min high and low round to the same ATM strike.
        sp = self._spot.reindex(allrows.index)
        atm_hi, atm_lo = (sp["high"] / 50).round(), (sp["low"] / 50).round()
        allrows["rolled"] = ~(atm_hi == atm_lo).to_numpy()
        allrows["day"] = allrows.index.date
        self._opts = {k: self._clean(g) for k, g in allrows.groupby(["day", "strike", "opt"])}

    @staticmethod
    def _clean(g: pd.DataFrame) -> pd.DataFrame:
        """One strike's candles for one day. Bars where ATM moved are rebuilt from the close:
        open = this strike's previous close (or the close), high/low = the larger/smaller of the two."""
        g = g.sort_values("rolled").pipe(lambda x: x[~x.index.duplicated(keep="first")]).sort_index().copy()
        prev_close = g["close"].shift(1)
        contiguous = pd.Series(g.index, index=g.index).diff() == BAR
        r = g["rolled"]
        g.loc[r, "open"] = prev_close.where(contiguous, g["close"])[r]
        g.loc[r, "high"] = g.loc[r, ["open", "close"]].max(axis=1)
        g.loc[r, "low"] = g.loc[r, ["open", "close"]].min(axis=1)
        return g[["open", "high", "low", "close"]]

    def _fetch_spot(self) -> pd.DataFrame:
        from engine.dhan_client import NIFTY_ID

        return self.client.intraday(NIFTY_ID, "IDX_I", "INDEX", datetime.combine(self.start, time(9, 0)),
                                    datetime.combine(self.end, time(15, 31)), interval=5)

    def _cached(self, name: str, fetch) -> pd.DataFrame:
        from engine.dhan_client import DhanError

        path = os.path.join(CACHE, name + ".parquet")
        if os.path.exists(path):
            return pd.read_parquet(path)
        try:
            df = fetch()
        except DhanError as exc:
            print(f"{name}: {exc}")
            if exc.not_subscribed:
                sys.exit(1)
            return pd.DataFrame()
        df.to_parquet(path)
        return df

    def weekly_expiry(self, d: date) -> Optional[date]:
        from engine.expiry import nifty_weekly_expiry

        return nifty_weekly_expiry(d)

    def spot(self, d: date) -> pd.DataFrame:
        if self._spot.empty:
            return self._spot
        return self._spot[self._spot.index.date == d].between_time(time(9, 15), time(15, 30))

    def option(self, expiry: date, strike: int, opt: str, d: date) -> pd.DataFrame:
        return self._opts.get((d, int(strike), opt), pd.DataFrame(columns=["open", "high", "low", "close"]))


# ---------- one day ----------

def price_at(df: pd.DataFrame, t: datetime, field: str = "open") -> Optional[float]:
    """Price at the first candle starting at or after t (that candle's open, or another field)."""
    after = df[df.index >= t]
    return float(after[field].iloc[0]) if not after.empty else None


def simulate_day(h: DhanHistory, d: date, qty: int, guard: float) -> Optional[dict]:
    spot = h.spot(d)
    if spot.empty:
        return None
    expiry = h.weekly_expiry(d)
    if expiry is None:
        return None
    start = datetime.combine(d, ENTRY)
    exit_at = datetime.combine(d, EXIT)

    legs: List[dict] = []          # every leg ever opened today
    realised = 0.0
    events: List[str] = []
    reentered = False

    def open_set(t: datetime, sl_pct: float, tag: str) -> bool:
        s = price_at(spot, t)
        if s is None:
            return False
        atm = int(round(s / 50.0) * 50)
        spec = [(atm + WING_PTS, "CE", "BUY"), (atm - WING_PTS, "PE", "BUY"), (atm, "CE", "SELL"), (atm, "PE", "SELL")]
        new = []
        for strike, opt, side in spec:
            df = h.option(expiry, strike, opt, d)
            p = price_at(df, t)
            if p is None:
                return False
            new.append({"strike": strike, "opt": opt, "side": side, "df": df, "entry": p, "exit": None,
                        "sl": p * (1 + sl_pct) if side == "SELL" else None, "set": tag})
        legs.extend(new)
        sells = [l for l in new if l["side"] == "SELL"]
        events.append(f"{t:%H:%M} {tag} NIFTY {s:,.0f} · sold {atm} CE ₹{sells[0]['entry']:.1f} + PE ₹{sells[1]['entry']:.1f}")
        return True

    def leg_pnl(l, px):
        return (px - l["entry"]) * qty if l["side"] == "BUY" else (l["entry"] - px) * qty

    def close(ls, prices: Dict[int, float], t, why):
        nonlocal realised
        for l in ls:
            if l["exit"] is None:
                l["exit"] = prices[id(l)]
                realised += leg_pnl(l, l["exit"])
        events.append(f"{t:%H:%M} {why}")

    if not open_set(start, SL_PCT, "Entry"):
        return None

    reentry_at = None
    t = start
    stopped = False
    while t < exit_at and not stopped:
        if reentry_at and t >= reentry_at:
            reentry_at = None
            reentered = open_set(t, REENTRY_SL_PCT, "Re-entry")
        live = [l for l in legs if l["exit"] is None]
        if live:
            bars = {id(l): l["df"].loc[t] if t in l["df"].index else None for l in live}
            # Stop-loss on a sold leg (candle high), fill at SL or the gap open
            hit = [l for l in live if l["side"] == "SELL" and bars[id(l)] is not None and bars[id(l)]["high"] >= l["sl"]]
            if hit:
                hit_ids = {id(l) for l in hit}
                prices = {}
                for l in live:
                    b = bars[id(l)]
                    if id(l) in hit_ids:
                        prices[id(l)] = max(l["sl"], float(b["open"]))
                    else:
                        prices[id(l)] = float(b["close"]) if b is not None else l["entry"]
                close(live, prices, t, f"SL {' & '.join(f'{l['strike']} {l['opt']}' for l in hit)} ({hit[0]['set']})")
                if hit[0]["set"] == "Entry" and t + BAR + REENTRY_WAIT < exit_at:
                    reentry_at = t + BAR + REENTRY_WAIT
            else:
                # Guard on mark-to-market at the candle close
                mtm = realised + sum(leg_pnl(l, float(bars[id(l)]["close"])) for l in live if bars[id(l)] is not None)
                if abs(mtm) >= guard:
                    close(live, {id(l): float(bars[id(l)]["close"]) if bars[id(l)] is not None else l["entry"] for l in live},
                          t, f"Guard {'target' if mtm > 0 else 'stop'} ₹{mtm:,.0f}")
                    stopped = True
        t += BAR

    live = [l for l in legs if l["exit"] is None]
    if live:
        close(live, {id(l): price_at(l["df"], exit_at) or float(l["df"]["close"].iloc[-1]) for l in live}, exit_at, "Time exit 15:25")

    buy_val = sum((l["entry"] if l["side"] == "BUY" else l["exit"]) * qty for l in legs)
    sell_val = sum((l["exit"] if l["side"] == "BUY" else l["entry"]) * qty for l in legs)
    charges = _charges(buy_val, sell_val, 2 * len(legs))
    return {"date": d, "expiry": expiry, "gross": realised, "charges": charges, "net": realised - charges,
            "reentry": reentered, "events": events}


# ---------- report ----------

def summarise(days: List[dict], capital: float) -> dict:
    net = pd.Series([r["net"] for r in days])
    wins, losses = net[net > 0], net[net <= 0]
    curve = net.cumsum()
    return {
        "days": len(days), "win_days": int((net > 0).sum()), "win_rate": round(100 * (net > 0).mean(), 1),
        "gross": round(sum(r["gross"] for r in days)), "charges": round(sum(r["charges"] for r in days)),
        "net": round(net.sum()), "roi_pct": round(100 * net.sum() / capital, 1),
        "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else None,
        "avg_win": round(wins.mean()) if len(wins) else 0, "avg_loss": round(losses.mean()) if len(losses) else 0,
        "best_day": round(net.max()), "worst_day": round(net.min()),
        "max_drawdown": round((curve - curve.cummax()).min()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--capital", type=float, default=100000)
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--expiry-code", type=int, default=1, help="Dhan weekly expiry code (1 = nearest)")
    ap.add_argument("--out", default=os.path.join(ROOT, "cache", "real_premium_backtest.json"))
    args = ap.parse_args()

    qty = 65 * args.lots
    guard = 0.10 * min(args.capital, 100000) * args.lots if args.capital <= 100000 else 15000
    h = DhanHistory(date.fromisoformat(args.start), date.fromisoformat(args.end), args.expiry_code)
    days = []
    for d in pd.bdate_range(args.start, args.end).date:
        try:
            r = simulate_day(h, d, qty, guard)
        except Exception as exc:
            print(f"{d} skipped: {exc}")
            continue
        if r:
            days.append(r)
            print(f"{d} {r['net']:>+9,.0f}  " + " | ".join(r["events"]))
    if not days:
        sys.exit("No days with data.")
    s = summarise(days, args.capital)
    print("\n" + json.dumps(s, indent=2))
    with open(args.out, "w") as f:
        json.dump({"summary": s, "days": [{**r, "date": str(r["date"]), "expiry": str(r["expiry"])} for r in days]}, f, indent=2)


if __name__ == "__main__":
    main()
