"""
Groww Trading API helper: access-token exchange, funds, quotes, and F&O orders.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from engine.expiry import nifty_weekly_expiry

logger = logging.getLogger("GrowwBroker")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_number(payload: Dict[str, Any], keys: List[str]) -> float:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return _num(payload[key])
        nested = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if nested and key in nested and nested[key] not in (None, ""):
            return _num(nested[key])
    return 0.0


def next_weekly_expiry(now: Optional[datetime] = None) -> str:
    """Current NIFTY weekly expiry (Tuesday since Sep 2025), rolling over after 15:30 on expiry day.

    Exchange holidays are not known here; check the NSE calendar on holiday weeks.
    """
    now = now or datetime.now()
    expiry = nifty_weekly_expiry(now.date())
    if expiry == now.date() and (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
        expiry = nifty_weekly_expiry(now.date() + timedelta(days=1))
    return expiry.strftime("%Y-%m-%d")


class GrowwBroker:
    name = "Groww"

    def __init__(self, api_key: str = "", api_secret: str = "", access_token: str = ""):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.access_token = (access_token or "").strip()
        self.client = None
        self.last_error = ""

    def connect(self) -> Tuple[bool, str]:
        if not self.api_key and not self.access_token:
            return False, "Groww API key is required."
        try:
            from growwapi import GrowwAPI

            token = self.access_token
            if self.api_key and self.api_secret:
                try:
                    resp = GrowwAPI.get_access_token(api_key=self.api_key, secret=self.api_secret)
                    if isinstance(resp, str) and resp:
                        token = resp
                    elif isinstance(resp, dict):
                        token = (
                            resp.get("token")
                            or resp.get("access_token")
                            or resp.get("accessToken")
                            or (resp.get("data") or {}).get("token")
                            or token
                        )
                    if token:
                        self.access_token = token
                except Exception as exc:
                    logger.warning("Access-token exchange failed, using API key as bearer: %s", exc)
                    token = token or self.api_key
            token = token or self.api_key
            self.client = GrowwAPI(token)
            return True, "Connected to Groww Trading API."
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Groww connect failed")
            return False, f"Groww connection failed: {exc}"

    def get_balance(self) -> Dict[str, Any]:
        if not self.client:
            ok, msg = self.connect()
            if not ok:
                return {"success": False, "error": msg, "balance": 0.0}

        errors: List[str] = []
        payload: Dict[str, Any] = {}
        try:
            payload = self.client.get_available_margin_details(timeout=12) or {}
        except Exception as exc:
            errors.append(f"margin: {exc}")
        try:
            profile = self.client.get_user_profile(timeout=12) or {}
        except Exception as exc:
            profile = {}
            errors.append(f"profile: {exc}")

        balance = _first_number(
            payload,
            [
                "clear_cash",
                "available_cash",
                "available_margin",
                "net_available",
                "equity_available",
                "fno_available",
                "cash_available",
                "availableBalance",
                "balance",
                "net_balance",
            ],
        )
        if balance <= 0:
            for nested_key in ("equity", "fno", "commodity", "margin_details", "margins"):
                nested = payload.get(nested_key)
                if isinstance(nested, dict):
                    balance = _first_number(
                        nested,
                        ["clear_cash", "available_cash", "available_margin", "net_available", "balance"],
                    )
                    if balance > 0:
                        break

        name = (
            profile.get("name")
            or profile.get("user_name")
            or profile.get("email")
            or (f"UCC: {profile['ucc']}" if profile.get("ucc") else "")
            or (f"ID: {profile['vendor_user_id'][:8]}…" if profile.get("vendor_user_id") else "")
            or "Groww Trader"
        )
        return {
            "success": True,
            "balance": round(balance, 2),
            "raw_margin": payload,
            "profile_name": name,
            "ucc": profile.get("ucc", ""),
            "active_segments": profile.get("active_segments", []),
            "warnings": errors,
        }

    def get_nifty_spot(self) -> float:
        if not self.client:
            self.connect()
        if self.client:
            attempts = [
                (("NSE_NIFTY",), "CASH"),
                (("NSE_NIFTY 50",), "CASH"),
                (("NSE_NIFTY",), "INDEX"),
                ("NSE_NIFTY", "CASH"),
            ]
            for symbols, segment in attempts:
                try:
                    data = self.client.get_ltp(exchange_trading_symbols=symbols, segment=segment, timeout=6)
                    if isinstance(data, dict):
                        for value in data.values():
                            if isinstance(value, dict):
                                ltp = _num(value.get("ltp") or value.get("last_price") or value.get("close"))
                                if ltp > 0:
                                    return ltp
                            elif isinstance(value, (int, float)):
                                if float(value) > 0:
                                    return float(value)
                except Exception as exc:
                    logger.debug("NIFTY LTP attempt failed: %s", exc)
            try:
                quote = self.client.get_quote("NIFTY", "NSE", "CASH", timeout=6)
                ltp = _num(quote.get("ltp") or quote.get("last_price") or quote.get("ohlc", {}).get("close"))
                if ltp > 0:
                    return ltp
            except Exception:
                pass

        # Robust real-time spot fallback via yfinance
        try:
            import yfinance as yf
            ticker = yf.Ticker("^NSEI")
            fast_info = getattr(ticker, "fast_info", None)
            if fast_info:
                p = getattr(fast_info, "last_price", None) or getattr(fast_info, "regular_market_price", None)
                if p and float(p) > 0:
                    return round(float(p), 2)
            hist = ticker.history(period="1d")
            if not hist.empty:
                return round(float(hist["Close"].iloc[-1]), 2)
        except Exception as exc:
            logger.debug("Yfinance spot fallback failed: %s", exc)

        return 24500.0

    def get_option_leg(self, strike: int, opt_type: str, expiry: Optional[str] = None) -> Dict[str, Any]:
        expiry = expiry or next_weekly_expiry()
        opt_type = opt_type.upper()
        if not self.client:
            self.connect()
        chain: Dict[str, Any] = {}
        try:
            chain = self.client.get_option_chain("NSE", "NIFTY", expiry, timeout=12) or {}
        except Exception as exc:
            logger.warning("Option chain fetch failed: %s", exc)

        rows = []
        for key in ("option_chain", "chains", "data", "strikes", "items"):
            if isinstance(chain.get(key), list):
                rows = chain[key]
                break
        if not rows and isinstance(chain, list):
            rows = chain

        for row in rows:
            if not isinstance(row, dict):
                continue
            row_strike = int(_num(row.get("strike") or row.get("strike_price") or row.get("strikePrice")))
            if row_strike != int(strike):
                continue
            side = row.get(opt_type.lower()) or row.get(opt_type) or row
            if not isinstance(side, dict):
                continue
            symbol = (
                side.get("trading_symbol")
                or side.get("tradingSymbol")
                or row.get("trading_symbol")
                or ""
            )
            ltp = _num(side.get("ltp") or side.get("last_price") or side.get("close"))
            if symbol:
                return {"trading_symbol": symbol, "ltp": ltp, "strike": strike, "opt_type": opt_type, "expiry": expiry}

        yy = expiry[2:4]
        mon = datetime.strptime(expiry, "%Y-%m-%d").strftime("%b").upper()
        symbol = f"NIFTY{yy}{mon}{int(strike)}{opt_type}"
        return {"trading_symbol": symbol, "ltp": 0.0, "strike": strike, "opt_type": opt_type, "expiry": expiry}

    def get_ltp(self, trading_symbol: str) -> float:
        if not self.client:
            self.connect()
        if not self.client or not trading_symbol:
            return 0.0
        try:
            data = self.client.get_ltp(
                exchange_trading_symbols=(f"NSE_{trading_symbol}",),
                segment="FNO",
                timeout=8,
            )
            if isinstance(data, dict):
                for value in data.values():
                    if isinstance(value, dict):
                        ltp = _num(value.get("ltp") or value.get("last_price"))
                        if ltp > 0:
                            return ltp
                    elif isinstance(value, (int, float)) and float(value) > 0:
                        return float(value)
        except Exception as exc:
            logger.debug("LTP failed for %s: %s", trading_symbol, exc)
        try:
            quote = self.client.get_quote(trading_symbol, "NSE", "FNO", timeout=8)
            return _num(quote.get("ltp") or quote.get("last_price"))
        except Exception:
            return 0.0

    def place_market_order(self, trading_symbol: str, qty: int, side: str, **_contract: Any) -> Dict[str, Any]:
        if not self.client:
            ok, msg = self.connect()
            if not ok:
                return {"success": False, "error": msg}
        side = "BUY" if str(side).upper() == "BUY" else "SELL"
        try:
            from growwapi import GrowwAPI

            resp = self.client.place_order(
                validity=GrowwAPI.VALIDITY_DAY,
                exchange=GrowwAPI.EXCHANGE_NSE,
                order_type=GrowwAPI.ORDER_TYPE_MARKET,
                product=GrowwAPI.PRODUCT_MIS,
                quantity=int(qty),
                segment=GrowwAPI.SEGMENT_FNO,
                trading_symbol=trading_symbol,
                transaction_type=GrowwAPI.TRANSACTION_TYPE_BUY if side == "BUY" else GrowwAPI.TRANSACTION_TYPE_SELL,
                timeout=15,
            )
            order_id = (
                resp.get("groww_order_id")
                or resp.get("order_id")
                or resp.get("orderId")
                or ""
            )
            return {"success": True, "order_id": order_id, "raw": resp, "side": side, "symbol": trading_symbol, "qty": qty}
        except Exception as exc:
            logger.exception("Order failed")
            return {"success": False, "error": str(exc), "side": side, "symbol": trading_symbol, "qty": qty}
