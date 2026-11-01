"""
NIFTY weekly option expiry calendar.

- Contracts expiring up to 28 Aug 2025 expired on THURSDAY.
- From 1 Sep 2025 (NSE circular FAOP68747) NIFTY weekly expiry is TUESDAY.
- If the expiry day is a market holiday, expiry moves to the previous trading day.

Holidays are only known when a set of trading days is supplied (the backtest passes
the dates present in its historical data). Without it, every weekday counts as a
trading day.
"""

from datetime import date, timedelta
from typing import Iterable, Optional, Set

TUESDAY_EXPIRY_FROM = date(2025, 9, 1)
THURSDAY, TUESDAY = 3, 1


def _next_weekday(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def _is_trading_day(d: date, trading_days: Optional[Set[date]]) -> bool:
    if d.weekday() >= 5:
        return False
    if trading_days and min(trading_days) <= d <= max(trading_days):
        return d in trading_days
    return True


def nifty_weekly_expiry(d: date, trading_days: Optional[Iterable[date]] = None) -> date:
    """Expiry date of the current-week NIFTY contract as seen on trading date `d`."""
    days = set(trading_days) if trading_days else None
    expiry = _next_weekday(d, THURSDAY)
    if expiry >= TUESDAY_EXPIRY_FROM:
        expiry = _next_weekday(d, TUESDAY)
    # Holiday on expiry day: contract expires on the previous trading day.
    adjusted = expiry
    while not _is_trading_day(adjusted, days) and adjusted > d:
        adjusted -= timedelta(days=1)
    # If that falls before today (today is past this week's expiry), roll to next week.
    if adjusted < d:
        return nifty_weekly_expiry(expiry + timedelta(days=1), days)
    return adjusted


def trading_days_until(d: date, expiry: date, trading_days: Optional[Iterable[date]] = None) -> int:
    """Number of full trading days after `d` up to and including `expiry`."""
    days = set(trading_days) if trading_days else None
    count, cur = 0, d + timedelta(days=1)
    while cur <= expiry:
        if _is_trading_day(cur, days):
            count += 1
        cur += timedelta(days=1)
    return count
