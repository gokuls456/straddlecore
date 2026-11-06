"""
Telegram alerts for the live algo: entries, stop-losses, guard hits, exits, failures.

Messages are sent on a background thread so a slow Telegram call never delays the
8-second trading loop. Repeated identical alerts (e.g. a recurring engine error)
are suppressed for a few minutes.
"""

from __future__ import annotations

import html
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
REPEAT_SUPPRESS_SECONDS = 600

_last_sent: Dict[str, float] = {}
_lock = threading.Lock()


def send_message(token: str, chat_id: str, text: str, timeout: float = 10, attempts: int = 3) -> Tuple[bool, str]:
    """Send one HTML-formatted message synchronously, retrying network blips and rate limits. Returns (ok, error)."""
    if not token or not chat_id:
        return False, "Telegram bot token and chat ID are required."
    err = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)
        try:
            resp = requests.post(
                API.format(token=token, method="sendMessage"),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=timeout,
            )
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            err = f"Telegram request failed: {exc}"
            continue
        if body.get("ok"):
            return True, ""
        err = body.get("description") or f"HTTP {resp.status_code}"
        # Bad token/chat or malformed text won't fix itself; only retry rate limits and server errors.
        if resp.status_code != 429 and resp.status_code < 500:
            break
    return False, err


def send_async(token: str, chat_id: str, text: str, dedupe_key: Optional[str] = None) -> None:
    """Fire-and-forget send; identical dedupe_keys within REPEAT_SUPPRESS_SECONDS are dropped."""
    if dedupe_key:
        with _lock:
            now = time.time()
            if now - _last_sent.get(dedupe_key, 0) < REPEAT_SUPPRESS_SECONDS:
                return
            _last_sent[dedupe_key] = now

    def _run():
        ok, err = send_message(token, chat_id, text)
        if not ok:
            logger.warning("Telegram alert not sent: %s", err)

    threading.Thread(target=_run, daemon=True).start()


def detect_chat_id(token: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Chat that most recently messaged the bot (the user sends it any message first)."""
    if not token:
        return None, "Bot token is required."
    try:
        resp = requests.get(API.format(token=token, method="getUpdates"), timeout=10)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return None, f"Telegram request failed: {exc}"
    if not body.get("ok"):
        return None, body.get("description") or f"HTTP {resp.status_code}"
    for update in reversed(body.get("result", [])):
        msg = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username") or ""
            return {"chat_id": str(chat["id"]), "name": name, "type": chat.get("type", "")}, ""
    # Name the bot this token belongs to, so a message sent to a different bot is easy to spot.
    bot = ""
    try:
        me = requests.get(API.format(token=token, method="getMe"), timeout=10).json().get("result") or {}
        bot = f"@{me['username']}" if me.get("username") else ""
    except (requests.RequestException, ValueError):
        pass
    if body.get("result") == [] and bot:
        return None, f"No messages yet for {bot}. Open https://t.me/{bot[1:]} , press Start (or send any message), then click Connect again."
    return None, "No messages yet. Open your bot in Telegram, press Start (or send any message), then try again."


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def format_manual_trade(mt: Dict[str, Any]) -> str:
    """The 'place this by hand' alert, mirroring the in-page popup."""
    lines: List[str] = ["🚨 <b>Algo entry failed: place this trade manually</b>", f"Reason: {esc(mt.get('reason', ''))}", ""]
    if mt.get("stuck_legs"):
        lines += [f"⚠️ <b>Already filled and NOT closed:</b> {esc(', '.join(mt['stuck_legs']))}. Check your Dhan positions first.", ""]
    for i, leg in enumerate(mt.get("legs", []), 1):
        icon = "🟢" if leg["side"] == "BUY" else "🔴"
        line = f"{i}. {icon} <b>{leg['side']}</b> <code>{esc(leg['trading_symbol'])}</code> × {leg['qty']} @ ~₹{leg['ref_price']:,.2f}"
        if leg.get("sl_price"):
            pct = round((leg.get("sl_pct") or 0) * 100)
            cond = f"buy back if ≥ ₹{leg['sl_price']:,.2f} (+{pct}%)" if leg["side"] == "SELL" else f"sell if ≤ ₹{leg['sl_price']:,.2f} (−{pct}%)"
            line += f"\n    SL: {cond}"
        else:
            line += "\n    SL: none (exits with the trade)"
        lines.append(line)
    lines += [
        "",
        "• Place hedge BUYs before SELLs.",
        "• Recompute SL from your actual fill price.",
        f"• Guard: exit all at +₹{mt.get('pl_target', 0):,.0f} / −₹{abs(mt.get('pl_stop', 0)):,.0f}.",
        f"• Any SL hit → close ALL legs. Else exit by {esc(mt.get('exit_time', '3:25 PM IST'))}.",
        "• The algo is halted and will not manage these.",
    ]
    return "\n".join(lines)
