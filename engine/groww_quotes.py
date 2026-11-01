"""
Live NIFTY option prices from the Groww Trade API, for the signal scanner (quotes only, no orders).

Two kinds of Groww API key work:
- TOTP key + TOTP secret: logs in by itself every day, so it suits GitHub Actions.
- Approval key + secret: works only on days the key is approved in the Groww app.

One option-chain call returns every strike, so prices are fetched at most once per
REFRESH_SECONDS and looked up from that snapshot. Any failure returns None and the
scanner falls back to its Black-Scholes estimate.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("GrowwQuotes")

REFRESH_SECONDS = 20
TOKEN_MAX_AGE = 4 * 3600   # Groww tokens expire daily at 6 AM; refresh well before
RETRY_LOGIN_AFTER = 300    # after a failed login, wait before trying again


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class GrowwQuotes:
    def __init__(self, api_key: str, secret: str = "", totp_secret: str = ""):
        self.api_key = (api_key or "").strip()
        self.secret = (secret or "").strip()
        self.totp_secret = (totp_secret or "").replace(" ", "").strip()
        self.client = None
        self.token_at = 0.0
        self.failed_at = 0.0
        self.last_error = ""
        self._chain: Dict[Tuple[int, str], float] = {}
        self._chain_expiry = ""
        self._chain_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key and (self.secret or self.totp_secret))

    def _login(self) -> bool:
        if self.client and time.time() - self.token_at < TOKEN_MAX_AGE:
            return True
        if time.time() - self.failed_at < RETRY_LOGIN_AFTER:
            return False
        try:
            from growwapi import GrowwAPI

            if self.totp_secret:
                import pyotp

                token = GrowwAPI.get_access_token(api_key=self.api_key, totp=pyotp.TOTP(self.totp_secret).now())
            else:
                token = GrowwAPI.get_access_token(api_key=self.api_key, secret=self.secret)
            if isinstance(token, dict):
                token = token.get("token") or token.get("access_token") or ""
            if not token:
                raise RuntimeError("Groww returned no access token")
            self.client = GrowwAPI(token)
            self.token_at = time.time()
            self.last_error = ""
            return True
        except Exception as exc:
            self.client = None
            self.failed_at = time.time()
            self.last_error = f"Groww login failed: {exc}"
            if not self.totp_secret:
                self.last_error += " (approval keys must be approved in the Groww app each day; a TOTP key avoids this)"
            logger.warning(self.last_error)
            return False

    def _load_chain(self, expiry: date) -> bool:
        exp = expiry.isoformat()
        if self._chain and self._chain_expiry == exp and time.time() - self._chain_at < REFRESH_SECONDS:
            return True
        if not self._login():
            return False
        try:
            raw = self.client.get_option_chain("NSE", "NIFTY", exp, timeout=12) or {}
        except Exception as exc:
            self.last_error = f"Groww option chain failed: {exc}"
            logger.warning(self.last_error)
            if "auth" in str(exc).lower() or "token" in str(exc).lower():
                self.client = None  # force a fresh login next time
            return False
        chain = self._parse(raw)
        if not chain:
            self.last_error = "Groww option chain was empty"
            return False
        self._chain, self._chain_expiry, self._chain_at = chain, exp, time.time()
        return True

    @staticmethod
    def _parse(raw: Any) -> Dict[Tuple[int, str], float]:
        """Accepts {"strikes": {"23050": {"CE": {...}, "PE": {...}}}} or a list of strike rows."""
        out: Dict[Tuple[int, str], float] = {}
        strikes = raw.get("strikes") if isinstance(raw, dict) else raw
        if isinstance(strikes, dict):
            rows = [(k, v) for k, v in strikes.items()]
        else:
            rows = []
            for key in ("option_chain", "chains", "data", "items"):
                if isinstance(raw, dict) and isinstance(raw.get(key), list):
                    strikes = raw[key]
                    break
            for row in strikes if isinstance(strikes, list) else []:
                if isinstance(row, dict):
                    rows.append((row.get("strike") or row.get("strike_price") or row.get("strikePrice"), row))
        for strike, row in rows:
            k = int(_num(strike))
            if not k or not isinstance(row, dict):
                continue
            for opt in ("CE", "PE"):
                side = row.get(opt) or row.get(opt.lower())
                if isinstance(side, dict):
                    ltp = _num(side.get("ltp") or side.get("last_price"))
                    if ltp > 0:
                        out[(k, opt)] = ltp
        return out

    def price(self, expiry: date, strike: int, opt_type: str) -> Optional[float]:
        if not self.configured or not self._load_chain(expiry):
            return None
        return self._chain.get((int(strike), opt_type.upper()))
