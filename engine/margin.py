"""
Capital needed to place one lot of each strategy, from Dhan at today's prices.

- Sellers (Iron Butterfly, straddles): Dhan's multi-leg margin calculator
  (POST /v2/margincalculator/multi), which includes the hedge benefit of the wings.
- Option buyers: premium paid = LTP × quantity for the legs bought.
- Gold Mini futures: Dhan's margin calculator for 1 lot of the front-month GOLDM contract.

Values are cached for CACHE_SECONDS. If Dhan can't be reached, the registry's rough
"capital_per_lot" is used and the source says so.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Dict, List, Tuple

from engine.dhan_client import DhanError, DhanQuotes, get_client, goldm_contracts
from engine.expiry import nifty_weekly_expiry
from engine.strategy_registry import get_strategy

logger = logging.getLogger("Margin")

CACHE_SECONDS = 600
NIFTY_QTY = 65
WING = 300

# strategy → legs (strike offset from ATM, CE/PE, BUY/SELL). Buyers' capital is the premium.
STRUCTURES: Dict[str, List[Tuple[int, str, str]]] = {
    "hedged_selling": [(0, "CE", "SELL"), (0, "PE", "SELL"), (WING, "CE", "BUY"), (-WING, "PE", "BUY")],
    "short_straddle": [(0, "CE", "SELL"), (0, "PE", "SELL")],
    "trend_hedged_straddle": [(0, "CE", "SELL"), (0, "PE", "SELL"), (WING, "CE", "BUY")],
    "option_buying": [(0, "CE", "BUY"), (0, "PE", "BUY")],
    "nifty_10_12_put": [(0, "CE", "BUY")],   # buys CE or PE by the break; the dearer one is used below
    "nifty_1pm_drop": [(0, "PE", "BUY")],
    "nifty_orb_945": [(0, "CE", "BUY")],     # buys CE or PE by the break; the dearer one is used below
    "nifty_gap_drive": [(0, "CE", "BUY")],   # buys CE or PE by the gap; the dearer one is used below
}

_cache: Dict[str, Tuple[float, Dict]] = {}


def _fallback(strategy_id: str, why: str) -> Dict:
    per_lot = float(get_strategy(strategy_id).get("capital_per_lot") or 30000.0)
    return {"per_lot": per_lot, "source": f"estimate ({why})", "live": False}


def _nifty(strategy_id: str) -> Dict:
    c = get_client()
    q = DhanQuotes(c)
    expiry = q.resolve_expiry(nifty_weekly_expiry(date.today()))
    snap = c.option_chain(expiry)
    chain, spot = snap["legs"], snap["spot"] or c.nifty_spot()
    atm = int(round(spot / 50.0) * 50)
    legs = STRUCTURES[strategy_id]
    rows = []
    for off, opt, side in legs:
        row = chain.get((atm + off, opt))
        if not row or not row.get("security_id"):
            raise DhanError(f"no {atm + off} {opt} in the option chain")
        rows.append((row, opt, side))

    if all(side == "BUY" for _, _, side in rows):
        premium = sum(r["ltp"] for r, _, _ in rows) * NIFTY_QTY
        if strategy_id in ("nifty_gap_drive", "nifty_orb_945", "nifty_10_12_put"):
            pe = chain.get((atm, "PE"))
            premium = max(premium, (pe["ltp"] if pe else 0) * NIFTY_QTY)
        return {"per_lot": round(premium, 2), "source": f"Dhan premium at NIFTY {spot:,.0f}, expiry {expiry:%d %b}",
                "live": True}

    body = {"dhanClientId": c.client_id, "includePosition": False, "includeOrders": False, "scripList": [
        {"exchangeSegment": "NSE_FNO", "transactionType": side, "quantity": NIFTY_QTY, "productType": "INTRADAY",
         "securityId": str(r["security_id"]), "price": r["ltp"], "triggerPrice": 0} for r, _, side in rows]}
    out = c.request("POST", "/margincalculator/multi", body)
    margin = float(out.get("totalMargin") or 0)
    if margin <= 0:
        raise DhanError("Dhan margin calculator returned no margin")
    # Buying the wings costs premium on top of the margin.
    wings = sum(r["ltp"] for r, _, side in rows if side == "BUY") * NIFTY_QTY
    return {"per_lot": round(margin + wings, 2),
            "source": f"Dhan margin calculator at NIFTY {spot:,.0f}, expiry {expiry:%d %b}", "live": True}


def _gold() -> Dict:
    c = get_client()
    today = date.today()
    front = next((x for x in goldm_contracts() if x[0] > today), None)
    if not front:
        raise DhanError("no listed GOLDM contract")
    expiry, sec_id, symbol = front
    ltp = c.ltp({"MCX_COMM": [sec_id]}).get(("MCX_COMM", sec_id), 0.0)
    out = c.request("POST", "/margincalculator", {
        "dhanClientId": c.client_id, "exchangeSegment": "MCX_COMM", "transactionType": "BUY", "quantity": 1,
        "productType": "INTRADAY", "securityId": str(sec_id), "price": ltp, "triggerPrice": 0})
    margin = float(out.get("totalMargin") or 0)
    if margin <= 0:
        raise DhanError("Dhan margin calculator returned no margin")
    return {"per_lot": round(margin, 2), "source": f"Dhan margin calculator, {symbol} at ₹{ltp:,.0f}", "live": True}


def capital_per_lot(strategy_id: str) -> Dict:
    """{"per_lot": ₹, "source": text, "live": bool} for one lot of the strategy."""
    hit = _cache.get(strategy_id)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if not get_client().configured:
        return _fallback(strategy_id, "no Dhan keys")
    try:
        out = _gold() if strategy_id == "gold_trend_futures" else _nifty(strategy_id) if strategy_id in STRUCTURES \
            else _fallback(strategy_id, "no structure")
    except Exception as exc:
        logger.warning("Live margin for %s failed: %s", strategy_id, exc)
        return _fallback(strategy_id, f"Dhan: {str(exc)[:80]}")
    _cache[strategy_id] = (time.time(), out)
    return out
