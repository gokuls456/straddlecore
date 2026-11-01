"""
Dhan (DhanHQ v2) order execution for NIFTY options.

Dhan's market-data APIs need a paid Data plan, so quotes (spot, option LTP) come
from a separate data broker (Groww) when one is supplied. Orders go to Dhan.
Option contracts are resolved to Dhan security IDs via Dhan's public scrip master.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache")

# (strike, "CE"/"PE") -> sorted [(expiry_date, security_id)], loaded once per day.
_nifty_index: Dict[Tuple[int, str], List[Tuple[date, str]]] = {}
_nifty_index_day: Optional[date] = None


def _load_nifty_options() -> Dict[Tuple[int, str], List[Tuple[date, str]]]:
    global _nifty_index, _nifty_index_day
    today = date.today()
    if _nifty_index and _nifty_index_day == today:
        return _nifty_index

    path = os.path.join(CACHE_DIR, f"dhan_scrip_{today.isoformat()}.csv")
    if not os.path.exists(path):
        os.makedirs(CACHE_DIR, exist_ok=True)
        resp = requests.get(SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)
        # Keep only today's copy.
        for name in os.listdir(CACHE_DIR):
            if name.startswith("dhan_scrip_") and name != os.path.basename(path):
                os.remove(os.path.join(CACHE_DIR, name))

    index: Dict[Tuple[int, str], List[Tuple[date, str]]] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["SEM_EXM_EXCH_ID"] != "NSE" or row["SEM_INSTRUMENT_NAME"] != "OPTIDX":
                continue
            if not row["SEM_TRADING_SYMBOL"].startswith("NIFTY-"):
                continue
            try:
                expiry = datetime.strptime(row["SEM_EXPIRY_DATE"][:10], "%Y-%m-%d").date()
                strike = int(float(row["SEM_STRIKE_PRICE"]))
            except ValueError:
                continue
            index.setdefault((strike, row["SEM_OPTION_TYPE"]), []).append((expiry, row["SEM_SMST_SECURITY_ID"]))
    for legs in index.values():
        legs.sort()
    _nifty_index, _nifty_index_day = index, today
    return index


def nifty_option_security_id(strike: int, opt_type: str, expiry: Optional[str] = None) -> Optional[str]:
    """Security ID for a NIFTY option: the requested expiry if listed, else the nearest one not yet expired."""
    legs = _load_nifty_options().get((int(strike), opt_type.upper()), [])
    today = date.today()
    wanted = datetime.strptime(expiry, "%Y-%m-%d").date() if expiry else None
    for exp, sec_id in legs:
        if wanted and exp == wanted:
            return sec_id
    for exp, sec_id in legs:
        if exp >= today:
            return sec_id
    return None


class DhanBroker:
    name = "Dhan"

    def __init__(self, client_id: str = "", access_token: str = "", data_broker: Any = None):
        self.client_id = (client_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.data_broker = data_broker  # e.g. GrowwBroker, used only for quotes
        self.last_error = ""

    def _headers(self) -> Dict[str, str]:
        return {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _error_text(resp: requests.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}: {resp.text[:200]}"
        if isinstance(body, dict):
            msg = body.get("errorMessage") or body.get("message")
            if not msg and isinstance(body.get("data"), dict):
                msg = "; ".join(f"{k}: {v}" for k, v in body["data"].items())
            if msg:
                code = body.get("errorCode")
                return f"{msg} ({code})" if code else str(msg)
        return f"HTTP {resp.status_code}"

    def connect(self) -> Tuple[bool, str]:
        if not self.client_id or not self.access_token:
            return False, "Dhan client ID and access token are required."
        try:
            resp = requests.get(f"{API_BASE}/profile", headers=self._headers(), timeout=10)
        except requests.RequestException as exc:
            self.last_error = str(exc)
            return False, f"Dhan connection failed: {exc}"
        if resp.status_code != 200:
            self.last_error = self._error_text(resp)
            return False, f"Dhan rejected the token: {self.last_error}"
        profile = resp.json()
        return True, f"Connected to Dhan (client {profile.get('dhanClientId')}, token valid till {profile.get('tokenValidity')})."

    def get_balance(self) -> Dict[str, Any]:
        try:
            funds = requests.get(f"{API_BASE}/fundlimit", headers=self._headers(), timeout=10)
            profile = requests.get(f"{API_BASE}/profile", headers=self._headers(), timeout=10)
        except requests.RequestException as exc:
            return {"success": False, "error": f"Dhan request failed: {exc}", "balance": 0.0}
        if funds.status_code != 200:
            return {"success": False, "error": self._error_text(funds), "balance": 0.0}
        f = funds.json()
        p = profile.json() if profile.status_code == 200 else {}
        warnings = []
        if p.get("dataPlan") != "Active":
            warnings.append("Dhan Data APIs not subscribed: quotes come from Groww.")
        return {
            "success": True,
            # Dhan's API spells this field "availabelBalance".
            "balance": round(float(f.get("availabelBalance") or f.get("availableBalance") or 0.0), 2),
            "profile_name": f"Dhan {self.client_id}",
            "ucc": self.client_id,
            "active_segments": [s.strip() for s in (p.get("activeSegment") or "").split(",") if s.strip()],
            "token_validity": p.get("tokenValidity", ""),
            "warnings": warnings,
        }

    # Quotes are delegated to the data broker (Dhan market data needs a paid plan).
    def get_nifty_spot(self) -> float:
        return self.data_broker.get_nifty_spot() if self.data_broker else 0.0

    def get_option_leg(self, strike: int, opt_type: str, expiry: Optional[str] = None) -> Dict[str, Any]:
        if self.data_broker:
            return self.data_broker.get_option_leg(strike, opt_type, expiry)
        return {"trading_symbol": "", "ltp": 0.0, "strike": strike, "opt_type": opt_type, "expiry": expiry}

    def get_ltp(self, trading_symbol: str) -> float:
        return self.data_broker.get_ltp(trading_symbol) if self.data_broker else 0.0

    def place_market_order(self, trading_symbol: str, qty: int, side: str, strike: Optional[int] = None,
                           opt_type: Optional[str] = None, expiry: Optional[str] = None) -> Dict[str, Any]:
        side = "BUY" if str(side).upper() == "BUY" else "SELL"
        if strike is None or opt_type is None:
            m = re.search(r"(\d{4,5})(CE|PE)$", trading_symbol or "")
            if not m:
                return {"success": False, "error": f"Cannot read strike/type from {trading_symbol}"}
            strike, opt_type = int(m.group(1)), m.group(2)
        try:
            security_id = nifty_option_security_id(strike, opt_type, expiry)
        except Exception as exc:
            return {"success": False, "error": f"Dhan scrip master unavailable: {exc}"}
        if not security_id:
            return {"success": False, "error": f"No Dhan contract for NIFTY {strike} {opt_type}"}

        payload = {
            "dhanClientId": self.client_id,
            "transactionType": side,
            "exchangeSegment": "NSE_FNO",
            "productType": "INTRADAY",
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(qty),
            "disclosedQuantity": 0,
            "price": 0,
            "triggerPrice": 0,
            "afterMarketOrder": False,
        }
        try:
            resp = requests.post(f"{API_BASE}/orders", headers=self._headers(), json=payload, timeout=15)
        except requests.RequestException as exc:
            logger.exception("Dhan order failed")
            return {"success": False, "error": str(exc), "side": side, "symbol": trading_symbol, "qty": qty}
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or not body.get("orderId") or body.get("orderStatus") == "REJECTED":
            return {"success": False, "error": self._error_text(resp) if resp.status_code != 200 else f"Order {body.get('orderStatus')}",
                    "side": side, "symbol": trading_symbol, "qty": qty, "raw": body}
        return {"success": True, "order_id": body["orderId"], "raw": body, "side": side, "symbol": trading_symbol, "qty": qty}
