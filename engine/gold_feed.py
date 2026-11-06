"""
MCX Gold Mini (GOLDM) futures candles for backtests, from Dhan (Data API plan required).

Real MCX prices: every GOLDM contract Dhan currently lists is fetched as 5-minute candles
(Dhan's intraday charts, 90 days per request) and stitched into one front-month series. Each
day uses the nearest contract that has not yet expired, so a day on or after a contract's
expiry date rolls to the next one.

Dhan's scrip master only lists live contracts, so history reaches back as far as the oldest
listed GOLDM contract has traded. Longer periods simply return the days that exist.
Each day is cut to the MCX session (09:00–23:30 IST).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Dict

import pandas as pd

from engine.dhan_client import get_client, goldm_contracts

logger = logging.getLogger("GoldFeed")

SESSION_START = time(9, 0)
SESSION_END = time(23, 30)
PERIOD_DAYS = {"1 month": 30, "3 months": 90, "6 months": 180, "1 year": 365, "2 years": 730, "3 years": 1095}
MIN_BARS = 100   # a full MCX session has ~174 five-minute bars


class GoldMiniFeed:
    def __init__(self, cache_dir: str = "cache"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def _front_month_series(self, start: date, end: date) -> pd.DataFrame:
        client = get_client()
        if not client.configured:
            raise RuntimeError("Dhan keys missing: save your Dhan Client ID and access token (or PIN + TOTP) first.")
        contracts = [c for c in goldm_contracts() if c[0] >= start]
        frames = []
        prev_expiry = start - timedelta(days=1)
        for expiry, sec_id, symbol in contracts:
            if prev_expiry >= end:
                break
            # This contract is the front month on days after the previous expiry, up to the day before its own.
            span_start = max(start, prev_expiry)
            span_end = min(end, expiry - timedelta(days=1))
            prev_expiry = expiry
            if span_end < span_start:
                continue
            df = client.intraday(sec_id, "MCX_COMM", "FUTCOM", datetime.combine(span_start, SESSION_START),
                                 datetime.combine(span_end, time(23, 59)), interval=5)
            if df.empty:
                continue
            df = df[["open", "high", "low", "close"]].astype(float)
            df = df[(df.index.date >= span_start) & (df.index.date <= span_end)]
            df["contract"] = symbol
            frames.append(df)
        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        return pd.concat(frames).sort_index()

    def get_historical_intraday_data(self, period: str = "1 month", **_ignored) -> Dict[date, pd.DataFrame]:
        days = PERIOD_DAYS.get(str(period).lower(), 30)
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        start = now_ist.date() - timedelta(days=days)
        cache_path = os.path.join(self.cache_dir, f"dhan_goldm_5m_{start}_{now_ist.date()}.parquet")

        combined = None
        if os.path.exists(cache_path):
            try:
                combined = pd.read_parquet(cache_path)
            except Exception as e:
                logger.warning(f"Failed to read gold cache: {e}")
        if combined is None:
            combined = self._front_month_series(start, now_ist.date())
            if combined.empty:
                return {}
            # Today's session isn't finished until 23:30; caching it would freeze a half day.
            if now_ist.time() >= time(23, 35):
                combined.to_parquet(cache_path)

        out: Dict[date, pd.DataFrame] = {}
        for d, day in combined.groupby(combined.index.date):
            if d < start or d.weekday() >= 5:
                continue
            if d == now_ist.date() and now_ist.time() < time(23, 35):
                continue  # incomplete session would invent the rest of the day
            day = day.between_time(SESSION_START, SESSION_END)[["open", "high", "low", "close"]]
            if len(day) >= MIN_BARS:
                out[d] = day
        return out
