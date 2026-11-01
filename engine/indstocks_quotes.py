"""
Live NIFTY option prices from the INDstocks (INDmoney) Flash API, for the signal scanner.
Quotes only: market-data calls need no static IP, and no orders are placed.

Login, either:
- Client ID + MPIN + TOTP secret: a fresh 24-hour token is generated automatically
  (POST /generate/token). Suits GitHub Actions.
- A ready access token from the INDstocks dashboard ("New Token"), valid 24 hours.

One option-chain call returns every strike near the money, so prices are fetched at most
once per REFRESH_SECONDS. Failures return None and the scanner uses its estimate.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Dict, Optional, Tuple

import requests

from engine.groww_quotes import _clean_totp_secret

logger = logging.getLogger("INDstocksQuotes")

BASE = "https://api.indstocks.com"
NIFTY_SCRIP = "40000001"     # NIFTY 50 index security ID
STRIKE_COUNT = 20            # strikes each side of ATM (±1,000 pts covers the ±300 wings)
REFRESH_SECONDS = 20
TOKEN_MAX_AGE = 20 * 3600    # tokens last 24 h
RETRY_LOGIN_AFTER = 900      # 5 failed logins lock the account for 15 min, so back off hard
RETRY_CHAIN_AFTER = 120


class INDstocksQuotes:
    name = "INDstocks"

    def __init__(self, client_id: str = "", mpin: str = "", totp_secret: str = "", access_token: str = ""):
        self.client_id = (client_id or "").strip()
        self.mpin = (mpin or "").strip()
        self.totp_secret = _clean_totp_secret(totp_secret)
        self.token = (access_token or "").strip()
        self.token_at = time.time() if self.token else 0.0
        self.fixed_token = bool(self.token)  # a pasted token can't be renewed
        self.failed_at = 0.0
        self.last_error = ""
        self._chain: Dict[Tuple[int, str], float] = {}
        self._chain_expiry = ""
        self._chain_at = 0.0
        self._chain_failed_at = 0.0
        self.underlying_ltp = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token or (self.client_id and self.mpin and self.totp_secret))

    def _login(self) -> bool:
        if self.token and (self.fixed_token or time.time() - self.token_at < TOKEN_MAX_AGE):
            return True
        if not (self.client_id and self.mpin and self.totp_secret):
            self.last_error = "INDstocks token expired; add Client ID, MPIN and TOTP secret for automatic login"
            return False
        if time.time() - self.failed_at < RETRY_LOGIN_AFTER:
            return False
        try:
            import pyotp

            if not re.fullmatch(r"[A-Z2-7]+", self.totp_secret):
                raise ValueError("TOTP secret is not valid (expected letters A–Z and digits 2–7)")
            resp = requests.post(
                f"{BASE}/generate/token",
                headers={"x-api-key": self.client_id, "Content-Type": "application/json"},
                json={"mpin": self.mpin, "totp": pyotp.TOTP(self.totp_secret).now()},
                timeout=15,
            )
            body = resp.json()
            token = (body.get("data") or {}).get("token") or body.get("token")
            if not token:
                raise RuntimeError(body.get("message") or f"HTTP {resp.status_code}")
            self.token, self.token_at, self.last_error = token, time.time(), ""
            return True
        except Exception as exc:
            self.token = ""
            self.failed_at = time.time()
            self.last_error = f"INDstocks login failed: {exc}"
            logger.warning(self.last_error)
            return False

    def _load_chain(self, expiry: date) -> bool:
        exp = expiry.isoformat()
        if self._chain and self._chain_expiry == exp and time.time() - self._chain_at < REFRESH_SECONDS:
            return True
        if time.time() - self._chain_failed_at < RETRY_CHAIN_AFTER or not self._login():
            return False
        try:
            resp = requests.get(
                f"{BASE}/market/option-chain",
                headers={"Authorization": self.token},
                params={"exchange": "NSE", "segment": "INDEX", "underlying-scrip": NIFTY_SCRIP,
                        "expiry": exp, "strike_count": STRIKE_COUNT},
                timeout=15,
            )
            body = resp.json()
            if resp.status_code != 200 or body.get("status") != "success":
                if resp.status_code in (401, 403) and not self.fixed_token:
                    self.token = ""  # expired or replaced: log in again
                raise RuntimeError(body.get("message") or f"HTTP {resp.status_code}")
        except Exception as exc:
            self._chain_failed_at = time.time()
            self.last_error = f"INDstocks option chain failed: {exc}"
            logger.warning(self.last_error)
            return False
        data = body.get("data") or {}
        chain: Dict[Tuple[int, str], float] = {}
        for strike, row in (data.get("strikes") or {}).items():
            for opt in ("CE", "PE"):
                side = (row or {}).get(opt.lower()) or {}
                ltp = float(side.get("last_price") or 0)
                if ltp > 0:
                    chain[(int(float(strike)), opt)] = ltp
        if not chain:
            self.last_error = "INDstocks option chain was empty"
            self._chain_failed_at = time.time()
            return False
        self._chain, self._chain_expiry, self._chain_at = chain, exp, time.time()
        self.underlying_ltp = float(data.get("underlying_ltp") or 0)
        return True

    def price(self, expiry: date, strike: int, opt_type: str) -> Optional[float]:
        if not self.configured or not self._load_chain(expiry):
            return None
        return self._chain.get((int(strike), opt_type.upper()))
