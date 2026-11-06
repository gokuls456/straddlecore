"""
Dhan (DhanHQ v2) broker for NIFTY options: funds, live quotes and market orders.

Quotes (NIFTY spot, option LTP, option chain) need the Dhan Data API plan; orders need a
static IP whitelisted in Dhan. Option contracts are resolved to Dhan security IDs from the
option chain, falling back to Dhan's public scrip master.

Trading symbols used here are "NIFTY-<expiry YYYY-MM-DD>-<strike>-<CE|PE>": Dhan's own
trading symbol is the same for every weekly expiry in a month, so it can't identify a leg.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Any, Dict, Optional, Tuple

from engine.dhan_client import (SUBSCRIBE_HINT, DhanClient, DhanError, DhanQuotes, get_client,
                                nifty_option_security_id)
from engine.expiry import next_weekly_expiry

logger = logging.getLogger("DhanBroker")

_SYMBOL = re.compile(r"^NIFTY-(\d{4}-\d{2}-\d{2})-(\d{4,6})-(CE|PE)$")
LTP_MAX_AGE = 2.0


def leg_symbol(expiry: date, strike: int, opt_type: str) -> str:
    return f"NIFTY-{expiry.isoformat()}-{int(strike)}-{opt_type.upper()}"


def parse_symbol(symbol: str) -> Optional[Tuple[date, int, str]]:
    m = _SYMBOL.match(symbol or "")
    return (date.fromisoformat(m.group(1)), int(m.group(2)), m.group(3)) if m else None


class DhanBroker:
    name = "Dhan"

    def __init__(self, client_id: str = "", access_token: str = "", pin: str = "", totp_secret: str = "",
                 client: Optional[DhanClient] = None):
        self.client = client or get_client(client_id=client_id, access_token=access_token,
                                           pin=pin, totp_secret=totp_secret)
        self.quotes = DhanQuotes(self.client)
        self.last_error = ""
        self._ids: Dict[str, str] = {}            # trading symbol → security id
        self._ltp: Dict[str, float] = {}
        self._ltp_at = 0.0

    @property
    def client_id(self) -> str:
        return self.client.client_id

    def connect(self) -> Tuple[bool, str]:
        if not self.client.configured:
            return False, "Dhan Client ID plus an access token (or PIN + TOTP secret) are required."
        try:
            p = self.client.profile()
        except DhanError as exc:
            self.last_error = str(exc)
            return False, f"Dhan rejected the login: {exc}"
        plan = "Data API active" if p.get("dataPlan") == "Active" else "Data API NOT active"
        return True, (f"Connected to Dhan (client {p.get('dhanClientId')}, token valid till "
                      f"{p.get('tokenValidity')}, {plan}).")

    def get_balance(self) -> Dict[str, Any]:
        try:
            f = self.client.fund_limit()
            p = self.client.profile()
        except DhanError as exc:
            return {"success": False, "error": str(exc), "balance": 0.0}
        warnings = []
        if p.get("dataPlan") != "Active":
            warnings.append(SUBSCRIBE_HINT)
        return {
            "success": True,
            # Dhan's API spells this field "availabelBalance".
            "balance": round(float(f.get("availabelBalance") or f.get("availableBalance") or 0.0), 2),
            "profile_name": f"Dhan {self.client_id}",
            "ucc": self.client_id,
            "active_segments": [s.strip() for s in (p.get("activeSegment") or "").split(",") if s.strip()],
            "token_validity": p.get("tokenValidity", ""),
            "data_plan": p.get("dataPlan", ""),
            "data_validity": p.get("dataValidity", ""),
            "warnings": warnings,
        }

    # ---------- quotes ----------

    def get_nifty_spot(self) -> float:
        try:
            return self.client.nifty_spot()
        except DhanError as exc:
            self.last_error = str(exc)
            return self.quotes.spot or 0.0

    def get_option_leg(self, strike: int, opt_type: str, expiry: Optional[str] = None) -> Dict[str, Any]:
        opt_type = opt_type.upper()
        wanted = date.fromisoformat(expiry or next_weekly_expiry())
        row = self.quotes.leg(wanted, strike, opt_type)
        exp = row["expiry"] if row else self.quotes.resolve_expiry(wanted)
        symbol = leg_symbol(exp, strike, opt_type)
        sec_id = str(row["security_id"]) if row and row.get("security_id") else None
        if not sec_id:
            try:
                sec_id = nifty_option_security_id(strike, opt_type, exp)
            except Exception as exc:
                self.last_error = f"Dhan scrip master unavailable: {exc}"
        if sec_id:
            self._ids[symbol] = sec_id
        if not row and self.quotes.last_error:
            self.last_error = self.quotes.last_error
        return {"trading_symbol": symbol, "ltp": float(row["ltp"]) if row else 0.0, "strike": strike,
                "opt_type": opt_type, "expiry": exp.isoformat(), "security_id": sec_id}

    def _security_id(self, symbol: str) -> Optional[str]:
        if symbol in self._ids:
            return self._ids[symbol]
        parsed = parse_symbol(symbol)
        if not parsed:
            return None
        exp, strike, opt = parsed
        sec_id = nifty_option_security_id(strike, opt, exp)
        if sec_id:
            self._ids[symbol] = sec_id
        return sec_id

    def get_ltp(self, trading_symbol: str) -> float:
        """LTP of one leg. All known legs are fetched in one call (Dhan allows 1 quote call per second)."""
        try:
            sec_id = self._security_id(trading_symbol)
        except Exception as exc:
            self.last_error = str(exc)
            return 0.0
        if not sec_id:
            return 0.0
        if trading_symbol not in self._ltp or time.time() - self._ltp_at > LTP_MAX_AGE:
            try:
                prices = self.client.ltp({"NSE_FNO": sorted({int(i) for i in self._ids.values()})})
            except DhanError as exc:
                self.last_error = str(exc)
                return self._ltp.get(trading_symbol, 0.0)
            self._ltp_at = time.time()
            for sym, sid in self._ids.items():
                px = prices.get(("NSE_FNO", int(sid)))
                if px:
                    self._ltp[sym] = px
        return self._ltp.get(trading_symbol, 0.0)

    # ---------- orders ----------

    def place_market_order(self, trading_symbol: str, qty: int, side: str, strike: Optional[int] = None,
                           opt_type: Optional[str] = None, expiry: Optional[str] = None) -> Dict[str, Any]:
        side = "BUY" if str(side).upper() == "BUY" else "SELL"
        base = {"side": side, "symbol": trading_symbol, "qty": qty}
        try:
            sec_id = self._security_id(trading_symbol)
            if not sec_id and strike is not None and opt_type:
                sec_id = nifty_option_security_id(strike, opt_type, date.fromisoformat(expiry) if expiry else None)
        except Exception as exc:
            return {"success": False, "error": f"Dhan scrip master unavailable: {exc}", **base}
        if not sec_id:
            return {"success": False, "error": f"No Dhan contract for {trading_symbol}", **base}
        try:
            body = self.client.place_order(sec_id, qty, side)
        except DhanError as exc:
            logger.warning("Dhan order failed: %s", exc)
            return {"success": False, "error": str(exc), **base}
        if not body.get("orderId") or body.get("orderStatus") == "REJECTED":
            return {"success": False, "error": f"Order {body.get('orderStatus') or 'not accepted'}", "raw": body, **base}
        return {"success": True, "order_id": body["orderId"], "raw": body, **base}
