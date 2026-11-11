"""
Market Data Feed Provider for NIFTY 50 Options Backtesting (data from Dhan only)
Supports:
- Real NIFTY 50 5-minute bars from Dhan's intraday charts API (last 5 years, needs the
  Dhan Data API plan), fetched in 90-day pieces
- Real data only: a day without Dhan 5-minute bars is left out (never synthesised)
- Local disk caching for instant subsequent evaluations
- Handling 'excluding last 30 days' toggle as seen in user's configuration
"""

import os
import json
import logging
from datetime import datetime, date, timedelta, time
from typing import Dict, Optional, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "cache")

class NiftyDataFeed:
    def __init__(self, cache_dir: str = CACHE_DIR):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache_path(self, period: str, exclude_last_30: bool) -> str:
        tag = f"nifty_{period}_ex30" if exclude_last_30 else f"nifty_{period}"
        return os.path.join(self.cache_dir, f"{tag}.parquet")

    def get_historical_intraday_data(self, 
                                     period: str = "1 year", 
                                     exclude_last_30: bool = False,
                                     start_date_str: Optional[str] = None,
                                     end_date_str: Optional[str] = None) -> Dict[date, pd.DataFrame]:
        """
        Returns a mapping of Date -> DataFrame containing intraday 5-minute bars.
        Columns: ['open', 'high', 'low', 'close']
        Index: DatetimeIndex
        """
        # Determine date bounds
        today = datetime.now().date()
        
        if start_date_str and end_date_str:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        else:
            period_days_map = {
                "1 month": 30,
                "3 months": 90,
                "6 months": 180,
                "1 year": 365,
                "2 years": 730,
                "3 years": 1095
            }
            total_days = period_days_map.get(period.lower(), 365)
            
            if exclude_last_30:
                end_date = today - timedelta(days=30)
                start_date = end_date - timedelta(days=total_days)
            else:
                end_date = today
                start_date = end_date - timedelta(days=total_days)

        cache_key = f"dhan_nifty_real_{start_date}_{end_date}.parquet"
        cache_path = os.path.join(self.cache_dir, cache_key)
        
        if os.path.exists(cache_path):
            try:
                cached_df = pd.read_parquet(cache_path)
                return self._group_by_day(cached_df)
            except Exception as e:
                logger.warning(f"Failed to read parquet cache: {e}")

        from engine.dhan_client import NIFTY_ID, get_client

        client = get_client()
        if not client.configured:
            raise RuntimeError("Dhan keys missing: save your Dhan Client ID and access token (or PIN + TOTP) first.")

        # Daily bars: ground-truth OHLC and the list of trading days
        daily_df = client.daily(NIFTY_ID, "IDX_I", "INDEX", start_date - timedelta(days=5), end_date)

        # Real 5-minute bars for the whole window (Dhan keeps 5 years)
        intraday_5m = client.intraday(NIFTY_ID, "IDX_I", "INDEX",
                                      datetime.combine(start_date, time(9, 0)),
                                      datetime.combine(end_date, time(15, 31)), interval=5)

        # Construct full intraday dataset day-by-day
        all_day_dfs = []
        # Today's session is incomplete until the close: synthesising it would invent the
        # rest of the day (future stop-losses and exits), so leave it out until 15:35 IST.
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        skip_today = now_ist.date() == today and now_ist.time() < time(15, 35)
        
        if daily_df.empty and intraday_5m.empty:
            raise RuntimeError(client.last_error or "Dhan returned no NIFTY candles for this period.")
        # Trading days = days Dhan has a daily or 5-minute bar for
        if not daily_df.empty:
            daily_df = daily_df.dropna(subset=['open', 'close', 'high', 'low'])
        days = {d.date() for d in daily_df.index} | (set(intraday_5m.index.date) if not intraday_5m.empty else set())
        valid_dates = sorted(d for d in days if start_date <= d <= end_date)

        for d in valid_dates:
            # Exclude weekends
            if d.weekday() >= 5:
                continue
            if skip_today and d == today:
                continue

            # Real Dhan 5m bars for this date?
            day_5m = intraday_5m[intraday_5m.index.date == d] if not intraday_5m.empty else pd.DataFrame()
            
            if len(day_5m) >= 40:
                # Filter strictly between 09:15 and 15:30
                day_5m_filtered = day_5m.between_time(time(9, 15), time(15, 30))
                if len(day_5m_filtered) >= 40:
                    all_day_dfs.append(day_5m_filtered[['open', 'high', 'low', 'close']].astype(float))
                    continue

            # No real 5-minute bars from Dhan for this day: leave it out (real data only).
            logger.info("Skipping %s: Dhan has no 5-minute NIFTY bars for it", d)

        if all_day_dfs:
            combined_df = pd.concat(all_day_dfs).sort_index()
            # Cache to parquet (not while today is left out, or the cache would stay without it)
            try:
                if not skip_today:
                    combined_df.to_parquet(cache_path)
            except Exception as e:
                logger.warning(f"Failed to cache parquet: {e}")
            return self._group_by_day(combined_df)
        
        return {}

    def _generate_realistic_intraday_day(self, d: date, d_open: float, d_high: float, d_low: float, d_close: float) -> pd.DataFrame:
        """
        Generates realistic 5-minute candles (75 bars from 09:15 to 15:30)
        constrained to match the exact daily Open, High, Low, and Close.
        Uses Brownian bridge with intraday volatility curve.
        """
        times = pd.date_range(start=f"{d} 09:15", end=f"{d} 15:25", freq='5min')
        n_bars = len(times)
        
        # Seed by date for deterministic, reproducible results
        seed_val = int(datetime.combine(d, time(0, 0)).timestamp()) % 2147483647
        rng = np.random.RandomState(seed_val)
        
        # Generate random walk from d_open to d_close
        steps = rng.normal(loc=0.0, scale=1.0, size=n_bars)
        walk = np.cumsum(steps)
        # Brownian bridge to force ending at (d_close - d_open)
        bridge = walk - (np.arange(n_bars) / (n_bars - 1)) * (walk[-1] - (d_close - d_open))
        prices = d_open + bridge

        # Rescale so the peak matches d_high and trough matches d_low
        cur_min = np.min(prices)
        cur_max = np.max(prices)
        
        if cur_max - cur_min > 1e-4:
            scale_high = (d_high - d_open) / max(1e-4, cur_max - d_open) if cur_max > d_open else 1.0
            scale_low = (d_open - d_low) / max(1e-4, d_open - cur_min) if cur_min < d_open else 1.0
            
            scaled_prices = np.zeros(n_bars)
            for i, p in enumerate(prices):
                if p >= d_open:
                    scaled_prices[i] = d_open + (p - d_open) * min(1.5, scale_high)
                else:
                    scaled_prices[i] = d_open - (d_open - p) * min(1.5, scale_low)
            prices = scaled_prices

        # Construct OHLC per 5-min candle
        bar_opens = prices.copy()
        bar_closes = np.roll(prices, -1)
        bar_closes[-1] = d_close
        
        noise_high = rng.uniform(0.5, 6.0, size=n_bars)
        noise_low = rng.uniform(0.5, 6.0, size=n_bars)
        
        bar_highs = np.maximum(bar_opens, bar_closes) + noise_high
        bar_lows = np.minimum(bar_opens, bar_closes) - noise_low
        
        # Ensure extremes don't exceed daily bounds
        bar_highs = np.minimum(bar_highs, d_high)
        bar_lows = np.maximum(bar_lows, d_low)
        
        # Force exact daily extremes on the max/min bar
        max_idx = np.argmax(prices)
        min_idx = np.argmin(prices)
        bar_highs[max_idx] = d_high
        bar_lows[min_idx] = d_low

        df = pd.DataFrame({
            'open': bar_opens,
            'high': bar_highs,
            'low': bar_lows,
            'close': bar_closes
        }, index=times)
        return df

    def _group_by_day(self, df: pd.DataFrame) -> Dict[date, pd.DataFrame]:
        grouped = {}
        for d, sub_df in df.groupby(df.index.date):
            grouped[d] = sub_df
        return grouped
