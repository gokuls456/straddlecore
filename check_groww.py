"""
Check which Groww API calls your TOTP key may use from this computer.

    .venv/bin/python check_groww.py

It reads the TOTP token and TOTP secret from the clipboard (copy each one, then press
Enter), logs in once, and prints OK / FAIL for each data call. The values are never
shown, saved or sent anywhere except Groww.
"""

import os
import subprocess

RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "groww_check_result.txt")
_lines = []


def report(line: str):
    """Print a result line and keep it for the results file (results only, never the keys)."""
    print(line)
    _lines.append(line)

from engine.groww_quotes import GrowwQuotes
from engine.signal_scanner import nifty_weekly_expiry, now_ist


def from_clipboard(label: str) -> str:
    input(f"Copy the {label} in Groww, then press Enter here... ")
    value = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    print(f"  got {len(value)} characters")
    return value


def main():
    key = from_clipboard("TOTP token (the long eyJ… key)")
    secret = from_clipboard("TOTP secret (code next to the QR, or the otpauth:// link)")
    subprocess.run(["pbcopy"], input="", text=True)  # don't leave the secret on the clipboard
    if key == secret:
        print("Both values are the same: copy the secret after the token.")
        return
    q = GrowwQuotes(key, totp_secret=secret)
    if not q._login():
        report(f"LOGIN       FAIL {q.last_error}")
        return
    report("LOGIN       OK")
    c, exp = q.client, nifty_weekly_expiry(now_ist().date()).isoformat()
    checks = [
        ("profile", lambda: c.get_user_profile(timeout=10).get("active_segments")),
        ("index ltp", lambda: c.get_ltp(exchange_trading_symbols=("NSE_NIFTY",), segment="CASH", timeout=10)),
        ("option chain", lambda: len((c.get_option_chain("NSE", "NIFTY", exp, timeout=12) or {}).get("strikes", {}))),
    ]
    for name, fn in checks:
        try:
            report(f"{name:<12} OK   {str(fn())[:120]}")
        except Exception as exc:
            report(f"{name:<12} FAIL {str(exc)[:160]}")


if __name__ == "__main__":
    try:
        main()
    finally:
        os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
        with open(RESULT_FILE, "w") as f:
            f.write("\n".join(_lines) + "\n")
        input("\nDone. Press Enter to close.")
