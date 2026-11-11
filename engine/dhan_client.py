"""
DhanHQ v2 client: the only market-data and broker connection this project uses.

Endpoints follow the official DhanHQ-py SDK (github.com/dhan-oss/DhanHQ-py) and
dhanhq.co/docs/v2. Raw HTTP is used so the shared rate limiter and token handling
below cover every caller (signal scanner, backtest feeds, live runner).

Login, either:
- Client ID + access token from web.dhan.co (valid 24 h, paste a new one daily), or
- Client ID + Dhan PIN + TOTP secret: a fresh 24 h token is generated automatically
  (POST auth.dhan.co/app/generateAccessToken). Suits GitHub Actions.

Market data (quotes, option chain, candles) needs the paid Dhan Data API plan;
without it every data call fails with "Data APIs not subscribed" (codes 806 / DH-902).
Order placement additionally needs a static IP whitelisted with Dhan (SEBI rule).

Credentials come from DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN / DHAN_PIN / DHAN_TOTP_SECRET
environment variables when set, else from cache/broker_config.json ("dhan" section).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

logger = logging.getLogger("Dhan")

API_BASE = "https://api.dhan.co/v2"
AUTH_BASE = "https://auth.dhan.co"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "cache")
BROKER_CONFIG_FILE = os.path.join(CACHE_DIR, "broker_config.json")
TOKEN_FILE = os.path.join(CACHE_DIR, "dhan_token.json")

# Security IDs (scrip master, segment IDX_I)
NIFTY_ID = 13
INDIA_VIX_ID = 21

# Minimum seconds between calls of one kind (Dhan: quote 1/s, data 5/s, option chain 1 per 3 s).
RATE_GAP = {"quote": 1.05, "data": 0.25, "chain": 3.05, "other": 0.06}
TOKEN_MAX_AGE = 23 * 3600
RETRY_LOGIN_AFTER = 300
INTRADAY_MAX_DAYS = 89     # Dhan serves at most 90 days of intraday candles per request
# Expired-options data: since 6 Nov 2026 Dhan serves 1 day per request (docs still say 30). In practice
# toDate is inclusive and fromDate..fromDate+1 is accepted, so each request covers two weekdays.

SUBSCRIBE_HINT = ("Dhan Data APIs are not active on this account. Subscribe at web.dhan.co → "
                  "Profile → Access DhanHQ APIs → Data APIs (₹499 + GST / month).")
NOT_SUBSCRIBED = {"806", "DH-902"}
TOKEN_ERRORS = {"807", "808", "809", "DH-901"}

IST_OFFSET = pd.Timedelta(hours=5, minutes=30)

_rate_lock = threading.Lock()
_last_call: Dict[str, float] = {}


class DhanError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def not_subscribed(self) -> bool:
        return self.code in NOT_SUBSCRIBED


def clean_totp_secret(value: str) -> str:
    """Accept the bare base32 secret, a full otpauth:// link from the QR code, or the secret
    with spaces/dashes/lowercase."""
    value = (value or "").strip()
    if value.lower().startswith("otpauth://"):
        value = (parse_qs(urlparse(value).query).get("secret") or [""])[0]
    return re.sub(r"[\s-]", "", value).upper().rstrip("=")


def _throttle(kind: str):
    gap = RATE_GAP.get(kind, RATE_GAP["other"])
    with _rate_lock:
        wait = _last_call.get(kind, 0.0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[kind] = time.time()


def _error_from(resp: requests.Response) -> DhanError:
    try:
        body = resp.json()
    except ValueError:
        return DhanError(f"HTTP {resp.status_code}: {resp.text[:200]}", status=resp.status_code)
    code, msg = "", ""
    if isinstance(body, dict):
        code = str(body.get("errorCode") or "")
        msg = body.get("errorMessage") or body.get("message") or ""
        remarks = body.get("remarks")
        if isinstance(remarks, dict):
            code = code or str(remarks.get("error_code") or "")
            msg = msg or remarks.get("error_message") or ""
        elif isinstance(remarks, str) and not msg:
            msg = remarks
        data = body.get("data")
        if isinstance(data, dict) and not msg:
            # Data API errors look like {"data": {"806": "Data APIs not subscribed"}, "status": "failed"}
            for k, v in data.items():
                code = code or str(k)
                msg = str(v)
                break
    if code in NOT_SUBSCRIBED:
        msg = SUBSCRIBE_HINT
    return DhanError(f"{msg or f'HTTP {resp.status_code}'}" + (f" ({code})" if code and code not in msg else ""),
                     code=code, status=resp.status_code)


def _to_ist_index(ts: Iterable[Any]) -> pd.DatetimeIndex:
    """Dhan candle timestamps are epoch seconds; return naive IST times."""
    return pd.DatetimeIndex(pd.to_datetime(list(ts), unit="s") + IST_OFFSET)


def candles_frame(raw: Dict[str, Any]) -> pd.DataFrame:
    """Columnar Dhan candles ({"open": [...], ..., "timestamp": [...]}) → OHLC DataFrame, naive IST index."""
    if not isinstance(raw, dict) or not raw.get("timestamp"):
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    cols = {k: raw[k] for k in ("open", "high", "low", "close", "volume", "open_interest", "strike", "spot", "iv", "oi")
            if isinstance(raw.get(k), list) and len(raw[k]) == len(raw["timestamp"])}
    df = pd.DataFrame(cols, index=_to_ist_index(raw["timestamp"]))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


# ---------------- credentials ----------------

def dhan_credentials(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Env vars win (GitHub Actions), then the "dhan" section of the given or saved config."""
    if cfg is None:
        try:
            with open(BROKER_CONFIG_FILE) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    d = (cfg or {}).get("dhan") or {}
    out = {}
    for key in ("client_id", "access_token", "pin", "totp_secret"):
        out[key] = (os.environ.get(f"DHAN_{key.upper()}") or d.get(key) or "").strip()
    return out


_clients: Dict[Tuple[str, ...], "DhanClient"] = {}
_clients_lock = threading.Lock()


def get_client(cfg: Optional[Dict[str, Any]] = None, **creds: str) -> "DhanClient":
    """One shared client per credential set, so the token and rate limits are shared."""
    c = creds or dhan_credentials(cfg)
    key = tuple(c.get(k, "") for k in ("client_id", "access_token", "pin", "totp_secret"))
    with _clients_lock:
        if key not in _clients:
            _clients[key] = DhanClient(*key)
        return _clients[key]


# ---------------- client ----------------

class DhanClient:
    name = "Dhan"

    def __init__(self, client_id: str = "", access_token: str = "", pin: str = "", totp_secret: str = ""):
        self.client_id = (client_id or "").strip()
        self.pin = (pin or "").strip()
        self.totp_secret = clean_totp_secret(totp_secret)
        self.token = (access_token or "").strip()
        self.token_at = time.time() if self.token else 0.0
        self.fixed_token = bool(self.token) and not self.can_auto_login
        self.failed_at = 0.0
        self.last_error = ""
        self._lock = threading.RLock()
        if not self.token and self.can_auto_login:
            self._load_saved_token()

    # ----- auth -----

    @property
    def can_auto_login(self) -> bool:
        return bool(self.client_id and self.pin and self.totp_secret)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and (self.token or self.can_auto_login))

    def _load_saved_token(self):
        try:
            with open(TOKEN_FILE) as f:
                saved = json.load(f)
        except Exception:
            return
        if saved.get("client_id") == self.client_id and time.time() - float(saved.get("at", 0)) < TOKEN_MAX_AGE:
            self.token, self.token_at = saved.get("token", ""), float(saved["at"])

    def _save_token(self):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(TOKEN_FILE, "w") as f:
                json.dump({"client_id": self.client_id, "token": self.token, "at": self.token_at}, f)
        except OSError:
            pass

    def generate_token(self) -> str:
        """Fresh 24 h access token from Client ID + PIN + TOTP (TOTP must be enabled in Dhan first)."""
        import pyotp

        if not re.fullmatch(r"[A-Z2-7]+", self.totp_secret):
            raise DhanError("TOTP secret is not valid: copy the text code shown under Dhan's TOTP QR code "
                            "(letters A–Z and digits 2–7).")
        resp = requests.post(f"{AUTH_BASE}/app/generateAccessToken",
                             params={"dhanClientId": self.client_id, "pin": self.pin,
                                     "totp": pyotp.TOTP(self.totp_secret).now()},
                             timeout=15)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        token = body.get("accessToken") if isinstance(body, dict) else None
        if resp.status_code != 200 or not token:
            raise DhanError(f"Dhan login failed: {body.get('message') or body.get('errorMessage') or _error_from(resp)}"
                            if isinstance(body, dict) else f"Dhan login failed: HTTP {resp.status_code}")
        return token

    def _ensure_token(self) -> str:
        with self._lock:
            if self.token and (self.fixed_token or time.time() - self.token_at < TOKEN_MAX_AGE):
                return self.token
            if not self.can_auto_login:
                if self.token:
                    return self.token  # pasted token: Dhan decides whether it is still valid
                raise DhanError("Dhan is not set up: add your Client ID and an access token "
                                "(or PIN + TOTP secret for automatic daily login).")
            if time.time() - self.failed_at < RETRY_LOGIN_AFTER:
                raise DhanError(self.last_error or "Dhan login failed recently; retrying shortly.")
            try:
                self.token = self.generate_token()
            except Exception as exc:
                self.failed_at = time.time()
                self.token = ""
                self.last_error = str(exc)
                logger.warning(self.last_error)
                raise DhanError(self.last_error) from exc
            self.token_at = time.time()
            self.last_error = ""
            self._save_token()
            return self.token

    def _headers(self) -> Dict[str, str]:
        return {"access-token": self._ensure_token(), "client-id": self.client_id,
                "Content-Type": "application/json", "Accept": "application/json"}

    # ----- HTTP -----

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, kind: str = "other",
                timeout: int = 20, _retry: bool = True, _busy: int = 0) -> Any:
        _throttle(kind)
        try:
            resp = requests.request(method, f"{API_BASE}{path}", headers=self._headers(),
                                    json=body if body is not None else None, timeout=timeout)
        except requests.RequestException as exc:
            self.last_error = f"Dhan request failed: {exc}"
            raise DhanError(self.last_error) from exc
        if resp.status_code == 200:
            try:
                out = resp.json()
            except ValueError:
                out = {}
            if isinstance(out, dict) and str(out.get("status", "")).lower() in ("failed", "failure", "error"):
                err = _error_from(resp)
                self.last_error = str(err)
                raise err
            return out
        err = _error_from(resp)
        # DH-904 / HTTP 429: too many requests (e.g. a background download plus a backtest). Back off and retry.
        if (err.code == "DH-904" or resp.status_code == 429) and _busy < 4:
            time.sleep(2 * (_busy + 1))
            return self.request(method, path, body, kind, timeout, _retry, _busy + 1)
        if (err.code in TOKEN_ERRORS or resp.status_code == 401) and self.can_auto_login and _retry:
            with self._lock:
                self.token, self.token_at = "", 0.0   # expired: log in again once
            return self.request(method, path, body, kind, timeout, _retry=False, _busy=_busy)
        if err.code in TOKEN_ERRORS or resp.status_code == 401:
            err = DhanError(f"Dhan access token expired or invalid ({err.code or resp.status_code}). Paste a new token, "
                            "or add your Dhan PIN + TOTP secret for automatic daily login.", err.code, resp.status_code)
        self.last_error = str(err)
        raise err

    # ----- account -----

    def profile(self) -> Dict[str, Any]:
        return self.request("GET", "/profile")

    def fund_limit(self) -> Dict[str, Any]:
        return self.request("GET", "/fundlimit")

    # ----- quotes -----

    def ltp(self, instruments: Dict[str, List[int]]) -> Dict[Tuple[str, int], float]:
        """{"IDX_I": [13], "NSE_FNO": [39963]} → {("IDX_I", 13): 24012.5, ...}"""
        body = {seg: [int(i) for i in ids] for seg, ids in instruments.items() if ids}
        raw = self.request("POST", "/marketfeed/ltp", body, kind="quote")
        out: Dict[Tuple[str, int], float] = {}
        for seg, rows in ((raw or {}).get("data") or {}).items():
            for sid, row in (rows or {}).items():
                px = float((row or {}).get("last_price") or 0)
                if px > 0:
                    out[(seg, int(sid))] = px
        return out

    def nifty_spot(self) -> float:
        return self.ltp({"IDX_I": [NIFTY_ID]}).get(("IDX_I", NIFTY_ID), 0.0)

    def expiry_list(self, underlying: int = NIFTY_ID, segment: str = "IDX_I") -> List[date]:
        raw = self.request("POST", "/optionchain/expirylist",
                           {"UnderlyingScrip": int(underlying), "UnderlyingSeg": segment}, kind="chain")
        return sorted(date.fromisoformat(str(x)[:10]) for x in (raw or {}).get("data") or [])

    def option_chain(self, expiry: date, underlying: int = NIFTY_ID, segment: str = "IDX_I") -> Dict[str, Any]:
        """{"spot": float, "legs": {(strike, "CE"): {"ltp", "security_id", "iv", "oi", "bid", "ask"}}}"""
        raw = self.request("POST", "/optionchain", {"UnderlyingScrip": int(underlying), "UnderlyingSeg": segment,
                                                    "Expiry": expiry.isoformat()}, kind="chain")
        data = (raw or {}).get("data") or {}
        legs: Dict[Tuple[int, str], Dict[str, Any]] = {}
        for strike, row in (data.get("oc") or {}).items():
            k = int(round(float(strike)))
            for opt in ("CE", "PE"):
                side = (row or {}).get(opt.lower())
                if not isinstance(side, dict):
                    continue
                legs[(k, opt)] = {
                    "ltp": float(side.get("last_price") or 0), "security_id": side.get("security_id"),
                    "iv": side.get("implied_volatility"), "oi": side.get("oi"),
                    "bid": side.get("top_bid_price"), "ask": side.get("top_ask_price"),
                }
        return {"spot": float(data.get("last_price") or 0), "legs": legs}

    # ----- candles -----

    def intraday(self, security_id: int, segment: str, instrument: str, start: datetime, end: datetime,
                 interval: int = 5, oi: bool = False) -> pd.DataFrame:
        """Minute candles (1/5/15/25/60), any span within the last 5 years; split into 90-day requests."""
        frames = []
        cur = start
        while cur < end:
            stop = min(end, cur + timedelta(days=INTRADAY_MAX_DAYS))
            raw = self.request("POST", "/charts/intraday", {
                "securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                "interval": str(interval), "oi": bool(oi),
                "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"), "toDate": stop.strftime("%Y-%m-%d %H:%M:%S"),
            }, kind="data", timeout=40)
            df = candles_frame(raw)
            if not df.empty:
                frames.append(df)
            cur = stop
        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    def daily(self, security_id: int, segment: str, instrument: str, start: date, end: date,
              expiry_code: Optional[int] = None) -> pd.DataFrame:
        """Daily candles from start to end (inclusive), indexed by date at midnight."""
        body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                "oi": False, "fromDate": start.isoformat(), "toDate": (end + timedelta(days=1)).isoformat()}
        if expiry_code is not None:
            body["expiryCode"] = int(expiry_code)
        df = candles_frame(self.request("POST", "/charts/historical", body, kind="data", timeout=40))
        if not df.empty:
            df.index = df.index.normalize()
        return df

    def rolling_option(self, strike: str, opt: str, start: date, end: date, interval: int = 5,
                       expiry_flag: str = "WEEK", expiry_code: int = 1, underlying: int = NIFTY_ID) -> pd.DataFrame:
        """Expired NIFTY option candles by ATM offset ("ATM", "ATM+3", "ATM-2"), ATM-10 … ATM+10.
        Each row carries the actual strike it priced at that moment. Fetched two weekdays per request."""
        frames = []
        side = "ce" if opt.upper() == "CE" else "pe"
        cur = start
        while cur <= end:
            if cur.weekday() >= 5:
                cur += timedelta(days=1)
                continue
            nxt = cur + timedelta(days=1)
            stop = nxt if (nxt <= end and nxt.weekday() < 5) else cur
            raw = self.request("POST", "/charts/rollingoption", {
                "exchangeSegment": "NSE_FNO", "interval": str(interval), "securityId": int(underlying),
                "instrument": "OPTIDX", "expiryFlag": expiry_flag, "expiryCode": int(expiry_code),
                "strike": strike, "drvOptionType": "CALL" if side == "ce" else "PUT",
                "requiredData": ["open", "high", "low", "close", "strike", "spot"],
                "fromDate": cur.isoformat(), "toDate": stop.isoformat(),
            }, kind="data", timeout=40)
            df = candles_frame(((raw or {}).get("data") or {}).get(side) or {})
            if not df.empty:
                frames.append(df[(df.index.date >= start) & (df.index.date <= end)])
            cur = stop + timedelta(days=1)
        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "strike", "spot"])
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    # ----- orders -----

    def place_order(self, security_id: str, qty: int, side: str, segment: str = "NSE_FNO",
                    product: str = "INTRADAY") -> Dict[str, Any]:
        payload = {
            "dhanClientId": self.client_id, "transactionType": "BUY" if side.upper() == "BUY" else "SELL",
            "exchangeSegment": segment, "productType": product, "orderType": "MARKET", "validity": "DAY",
            "securityId": str(security_id), "quantity": int(qty), "disclosedQuantity": 0,
            "price": 0, "triggerPrice": 0, "afterMarketOrder": False,
        }
        return self.request("POST", "/orders", payload, kind="other", timeout=15)


# ---------------- scrip master ----------------

_scrip_lock = threading.Lock()
_scrip_rows: List[Dict[str, str]] = []
_scrip_day: Optional[date] = None


def scrip_master() -> List[Dict[str, str]]:
    """Dhan's public instrument list (no login needed), downloaded once per day. Only the rows this
    project uses are kept: NIFTY index options, MCX GOLDM futures and NSE indices."""
    global _scrip_rows, _scrip_day
    today = date.today()
    with _scrip_lock:
        if _scrip_rows and _scrip_day == today:
            return _scrip_rows
        path = os.path.join(CACHE_DIR, f"dhan_scrip_{today.isoformat()}.csv")
        if not os.path.exists(path):
            os.makedirs(CACHE_DIR, exist_ok=True)
            resp = requests.get(SCRIP_MASTER_URL, timeout=90)
            resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(resp.content)
            for name in os.listdir(CACHE_DIR):
                if name.startswith("dhan_scrip_") and name != os.path.basename(path):
                    os.remove(os.path.join(CACHE_DIR, name))
        rows = []
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                sym = r.get("SEM_TRADING_SYMBOL", "")
                inst = r.get("SEM_INSTRUMENT_NAME", "")
                if (inst == "OPTIDX" and r.get("SEM_EXM_EXCH_ID") == "NSE" and sym.startswith("NIFTY-")) or \
                   (inst == "FUTCOM" and sym.startswith("GOLDM-")) or inst == "INDEX":
                    rows.append(r)
        _scrip_rows, _scrip_day = rows, today
        return rows


def _expiry_of(row: Dict[str, str]) -> Optional[date]:
    try:
        return datetime.strptime(row["SEM_EXPIRY_DATE"][:10], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return None


def nifty_option_security_id(strike: int, opt_type: str, expiry: Optional[date] = None) -> Optional[str]:
    """Security ID for a NIFTY option: the given expiry if listed, else the nearest one not yet expired."""
    opt_type = opt_type.upper()
    legs = []
    for r in scrip_master():
        if r["SEM_INSTRUMENT_NAME"] != "OPTIDX" or r.get("SEM_OPTION_TYPE") != opt_type:
            continue
        try:
            if int(float(r["SEM_STRIKE_PRICE"])) != int(strike):
                continue
        except ValueError:
            continue
        exp = _expiry_of(r)
        if exp:
            legs.append((exp, r["SEM_SMST_SECURITY_ID"]))
    legs.sort()
    for exp, sid in legs:
        if expiry and exp == expiry:
            return sid
    today = date.today()
    return next((sid for exp, sid in legs if exp >= today), None)


def goldm_contracts() -> List[Tuple[date, int, str]]:
    """Listed MCX Gold Mini futures as (expiry, security_id, trading_symbol), nearest first."""
    out = []
    for r in scrip_master():
        if r["SEM_INSTRUMENT_NAME"] == "FUTCOM" and r["SEM_TRADING_SYMBOL"].startswith("GOLDM-"):
            exp = _expiry_of(r)
            if exp:
                out.append((exp, int(r["SEM_SMST_SECURITY_ID"]), r["SEM_TRADING_SYMBOL"]))
    return sorted(out)


# ---------------- option-chain snapshot for quotes ----------------

class DhanQuotes:
    """Live NIFTY option prices from one Dhan option-chain call (all strikes), refreshed at most every
    REFRESH seconds. Any failure returns None so callers can fall back to an estimate."""

    name = "Dhan"
    REFRESH = 10
    RETRY_AFTER = 60
    EXPIRY_REFRESH = 3600

    def __init__(self, client: DhanClient):
        self.client = client
        self.last_error = ""
        self._chain: Dict[Tuple[int, str], Dict[str, Any]] = {}
        self._chain_expiry: Optional[date] = None
        self._chain_at = 0.0
        self._failed_at = 0.0
        self._expiries: List[date] = []
        self._expiries_at = 0.0
        self.spot = 0.0

    @property
    def configured(self) -> bool:
        return self.client.configured

    def resolve_expiry(self, wanted: date) -> date:
        """The listed Dhan expiry for the week of `wanted` (handles holiday-shifted expiries)."""
        try:
            if not self._expiries or time.time() - self._expiries_at > self.EXPIRY_REFRESH:
                self._expiries, self._expiries_at = self.client.expiry_list(), time.time()
        except DhanError as exc:
            self.last_error = str(exc)
            return wanted
        today = date.today()
        live = [e for e in self._expiries if e >= today]
        if wanted in live:
            return wanted
        before = [e for e in live if e <= wanted]
        if before:
            return before[-1]
        return live[0] if live else wanted

    def _load(self, expiry: date) -> bool:
        if self._chain and self._chain_expiry == expiry and time.time() - self._chain_at < self.REFRESH:
            return True
        if time.time() - self._failed_at < self.RETRY_AFTER:
            return False
        try:
            snap = self.client.option_chain(expiry)
        except DhanError as exc:
            self._failed_at = time.time()
            self.last_error = str(exc)
            logger.warning("Dhan option chain failed: %s", exc)
            return False
        if not snap["legs"]:
            self._failed_at = time.time()
            self.last_error = f"Dhan option chain for {expiry} was empty"
            return False
        self._chain, self._chain_expiry, self._chain_at = snap["legs"], expiry, time.time()
        self.spot = snap["spot"] or self.spot
        self.last_error = ""
        return True

    def leg(self, expiry: date, strike: int, opt_type: str) -> Optional[Dict[str, Any]]:
        if not self.configured:
            return None
        exp = self.resolve_expiry(expiry)
        if not self._load(exp):
            return None
        row = self._chain.get((int(strike), opt_type.upper()))
        return dict(row, expiry=exp) if row else None

    def price(self, expiry: date, strike: int, opt_type: str) -> Optional[float]:
        row = self.leg(expiry, strike, opt_type)
        return row["ltp"] if row and row["ltp"] > 0 else None


def nifty_candles(start: str, end: str, minutes: int = 3) -> pd.DataFrame:
    """NIFTY OHLC candles of any minute size between two YYYY-MM-DD dates (inclusive), built from
    Dhan 1-minute bars (Dhan serves 1/5/15/25/60 natively). For the standalone research scripts."""
    client = get_client()
    if not client.configured:
        raise DhanError("Dhan keys missing: save them in the app or set DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN.")
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    df = client.intraday(NIFTY_ID, "IDX_I", "INDEX", datetime.combine(s, datetime.min.time()).replace(hour=9),
                         datetime.combine(e, datetime.min.time()).replace(hour=15, minute=31), interval=1)
    if df.empty:
        return df
    df = df[["open", "high", "low", "close"]].astype(float).between_time("09:15", "15:29")
    if minutes == 1:
        return df
    out = df.resample(f"{minutes}min", origin="start_day", offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    out.index.name = "datetime"
    return out
