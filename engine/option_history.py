"""
Real NIFTY weekly option prices for backtests, from Dhan's expired-options candles
(POST /v2/charts/rollingoption, Data API plan). No modelled prices: a day without real
option candles is left out of the backtest.

Dhan serves these candles by offset from ATM (ATM-10 … ATM+10, i.e. ±500 points) for the
nearest weekly expiry, with the actual strike on every candle. All 21 offsets × CE/PE are
fetched for each day and regrouped by strike, so a leg keeps its fixed strike all day.
One request covers two weekdays, so a day costs ~21 requests (~5 s); a year ~20 minutes
the first time. Each day is cached in cache/dhan_opt/YYYY-MM-DD.parquet and never refetched.

Rolled candles: the strike label is the ATM+offset strike at the candle's close. When NIFTY
crosses an ATM boundary inside a candle, that candle's open/high/low mix two strikes, so only
its close is kept (open = the strike's previous close, high/low = max/min of open and close).
Checked against live contracts: closes match within ~₹2.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("OptionHistory")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache", "dhan_opt")
OFFSETS = range(-10, 11)
BAR = timedelta(minutes=5)
COLS = ["open", "high", "low", "close", "strike", "spot", "opt"]


class NoRealPrice(Exception):
    """Dhan has no candle for this strike at this time (outside ATM±10 or missing data)."""


def _path(d: date) -> str:
    return os.path.join(CACHE, f"{d.isoformat()}.parquet")


def has_day(d: date) -> bool:
    return os.path.exists(_path(d))


_empty_recent: set = set()   # recent days Dhan returned nothing for (not cached; not retried this session)


def missing_days(days: Iterable[date]) -> List[date]:
    return [d for d in days if d.weekday() < 5 and not has_day(d) and d not in _empty_recent]


def _fetch_pair(client, d1: date, d2: date) -> Dict[date, pd.DataFrame]:
    """All offsets × CE/PE for d1..d2 (one request each). Raises DhanError on API errors."""
    frames = []
    for opt in ("CE", "PE"):
        for off in OFFSETS:
            label = "ATM" if off == 0 else f"ATM{off:+d}"
            df = client.rolling_option(label, opt, d1, d2, interval=5)
            if not df.empty and "strike" in df:
                frames.append(df.assign(opt=opt))
    allrows = pd.concat(frames) if frames else pd.DataFrame(columns=COLS)
    out = {}
    for d in (d1, d2):
        day = allrows[allrows.index.date == d] if len(allrows) else allrows
        out[d] = day.reindex(columns=COLS)
    return out


def fetch_days(days: Iterable[date], client=None, progress=None) -> int:
    """Download and cache the given days (newest first). Returns how many were saved."""
    from engine.dhan_client import get_client

    client = client or get_client()
    todo = sorted(set(missing_days(days)), reverse=True)
    os.makedirs(CACHE, exist_ok=True)
    saved, i = 0, 0
    while i < len(todo):
        d2 = todo[i]
        # Pair with the previous weekday when it's also wanted: one request covers both.
        d1 = todo[i + 1] if i + 1 < len(todo) and (d2 - todo[i + 1]).days == 1 else d2
        for d, df in _fetch_pair(client, d1, d2).items():
            if d in todo and not has_day(d):
                if d >= date.today() or (df.empty and (date.today() - d).days <= 3):
                    if df.empty:
                        _empty_recent.add(d)
                    continue          # today's candles are still forming / Dhan may not have published yet
                df.to_parquet(_path(d))   # an empty file marks a holiday / no data
                saved += 1
        i += 2 if d1 != d2 else 1
        if progress:
            progress(min(i, len(todo)), len(todo))
    return saved


class DayBook:
    """Real option candles for one day, by (strike, CE/PE)."""

    def __init__(self, d: date, legs: Dict[Tuple[int, str], pd.DataFrame]):
        self.day = d
        self.legs = legs

    def candles(self, strike: int, opt: str) -> pd.DataFrame:
        df = self.legs.get((int(strike), opt))
        if df is None or df.empty:
            raise NoRealPrice(f"{self.day} {strike} {opt}: no Dhan candles")
        return df

    def bar(self, strike: int, opt: str, t: datetime) -> Optional[pd.Series]:
        """The candle starting at t, or None if Dhan has none at that time."""
        df = self.legs.get((int(strike), opt))
        if df is None or t not in df.index:
            return None
        return df.loc[t]

    def price(self, strike: int, opt: str, t: datetime, field: str = "close") -> float:
        """field="open": open of the first candle at/after t (an order placed at t).
        field="close": close of the last candle at/before t (the price at the end of that bar)."""
        df = self.candles(strike, opt)
        if field == "open":
            after = df[df.index >= t]
            if after.empty:
                raise NoRealPrice(f"{self.day} {strike} {opt}: no candle at/after {t:%H:%M}")
            return float(after["open"].iloc[0])
        before = df[df.index <= t]
        if before.empty:
            raise NoRealPrice(f"{self.day} {strike} {opt}: no candle at/before {t:%H:%M}")
        return float(before[field].iloc[-1])


def _clean(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("rolled").pipe(lambda x: x[~x.index.duplicated(keep="first")]).sort_index().copy()
    prev_close = g["close"].shift(1)
    contiguous = pd.Series(g.index, index=g.index).diff() == BAR
    r = g["rolled"]
    g.loc[r, "open"] = prev_close.where(contiguous, g["close"])[r]
    g.loc[r, "high"] = g.loc[r, ["open", "close"]].max(axis=1)
    g.loc[r, "low"] = g.loc[r, ["open", "close"]].min(axis=1)
    return g[["open", "high", "low", "close"]]


_books: Dict[date, Optional[DayBook]] = {}


def load_day(d: date, spot: Optional[pd.DataFrame] = None) -> Optional[DayBook]:
    """The day's real option candles, or None if not downloaded / Dhan had none."""
    if d in _books:
        return _books[d]
    if not has_day(d):
        return None
    rows = pd.read_parquet(_path(d))
    if rows.empty:
        _books[d] = None
        return None
    rows = rows.dropna(subset=["close", "strike"]).copy()
    rows["strike"] = rows["strike"].round().astype(int)
    # Clean candle = NIFTY's 5-min high and low round to the same ATM strike.
    if spot is not None and not spot.empty:
        sp = spot.reindex(rows.index)
        rolled = ~((sp["high"] / 50).round() == (sp["low"] / 50).round()).to_numpy()
        rolled &= sp["high"].notna().to_numpy()
    else:
        rolled = pd.Series(False, index=rows.index).to_numpy()
    rows["rolled"] = rolled
    book = DayBook(d, {(int(k), o): _clean(g) for (k, o), g in rows.groupby(["strike", "opt"])})
    _books[d] = book
    return book


# ---------- background download, with progress for the UI ----------

_state = {"running": False, "done": 0, "total": 0, "error": "", "started": None, "finished": None}
_lock = threading.Lock()


def status() -> Dict:
    with _lock:
        s = dict(_state)
    if s["running"] and s["done"] and s["started"]:
        per = (datetime.now() - s["started"]).total_seconds() / s["done"]
        s["eta_seconds"] = round(per * (s["total"] - s["done"]))
    s["cached_days"] = len([f for f in os.listdir(CACHE)]) if os.path.isdir(CACHE) else 0
    return s


def start_download(days: Iterable[date]) -> bool:
    """Download missing days in a background thread. False if one is already running or nothing is missing."""
    todo = missing_days(days)
    todo = [d for d in todo if d < date.today()]
    with _lock:
        if _state["running"] or not todo:
            return False
        _state.update(running=True, done=0, total=len(todo), error="", started=datetime.now(), finished=None)

    def progress(done, total):
        with _lock:
            _state.update(done=done, total=total)

    def run():
        try:
            fetch_days(todo, progress=progress)
        except Exception as exc:  # DhanError (plan / token) or network
            logger.warning("Option history download stopped: %s", exc)
            with _lock:
                _state["error"] = str(exc)[:300]
        finally:
            with _lock:
                _state.update(running=False, finished=datetime.now())

    threading.Thread(target=run, name="option-history", daemon=True).start()
    return True
