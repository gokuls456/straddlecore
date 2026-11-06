"""
Save and check your Dhan keys (the only broker / data source this project uses).

    .venv/bin/python scripts/set_dhan_keys.py          save keys, then check them
    .venv/bin/python scripts/set_dhan_keys.py --check  only check the saved keys

Each value is read from the clipboard (copy it when asked, then press Enter), so nothing
is typed or printed. Keys are stored in cache/broker_config.json ("dhan" section, git-ignored).

Two ways to log in:
  token  Client ID + access token from web.dhan.co (valid 24 h; paste a new one daily)
  totp   Client ID + Dhan PIN + TOTP secret: a new token is made automatically every day
         (enable TOTP first: web.dhan.co → Profile → Access DhanHQ APIs → Setup TOTP)

The check prints OK / FAIL for login, the Data API plan, NIFTY spot, India VIX, option chain
and intraday candles. "Data APIs not subscribed" means the ₹499/month Data API plan is off.
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG = os.path.join(ROOT, "cache", "broker_config.json")

from engine.dhan_client import INDIA_VIX_ID, NIFTY_ID, DhanClient, DhanQuotes, clean_totp_secret  # noqa: E402
from engine.expiry import nifty_weekly_expiry  # noqa: E402


def from_clipboard(what: str) -> str:
    input(f"Copy your {what}, then press Enter here: ")
    value = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if not value:
        sys.exit("The clipboard was empty. Nothing was saved.")
    print(f"  got it ({len(value)} characters)")
    return value


def load_cfg() -> dict:
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def check(c: DhanClient) -> bool:
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            print(f"{name:<14} OK   {str(fn())[:110]}")
        except Exception as exc:
            ok = False
            print(f"{name:<14} FAIL {str(exc)[:200]}")

    step("login/profile", lambda: {k: v for k, v in c.profile().items()
                                   if k in ("dhanClientId", "tokenValidity", "activeSegment", "dataPlan", "dataValidity")})
    step("NIFTY spot", lambda: c.nifty_spot())
    step("India VIX", lambda: c.ltp({"IDX_I": [INDIA_VIX_ID]}).get(("IDX_I", INDIA_VIX_ID)))
    q = DhanQuotes(c)
    exp = q.resolve_expiry(nifty_weekly_expiry(date.today()))
    step("option chain", lambda: f"{len(c.option_chain(exp)['legs'])} legs for expiry {exp}")
    now = datetime.now()
    step("5-min candles", lambda: f"{len(c.intraday(NIFTY_ID, 'IDX_I', 'INDEX', now - timedelta(days=5), now))} bars (last 5 days)")
    return ok


def main():
    cfg = load_cfg()
    if "--check" not in sys.argv:
        client_id = from_clipboard("Dhan Client ID")
        kind = input("Log in with a pasted access token or automatically with PIN + TOTP? Type token or totp: ").strip().lower()
        d = {"client_id": client_id}
        if kind.startswith("t") and kind != "token":
            d["pin"] = from_clipboard("Dhan PIN (the 4-6 digit login PIN)")
            d["totp_secret"] = clean_totp_secret(from_clipboard("TOTP secret (text under the QR code, or the otpauth:// link)"))
            d["access_token"] = ""
        else:
            d["access_token"] = from_clipboard("Dhan access token")
            d["pin"], d["totp_secret"] = "", ""
        subprocess.run(["pbcopy"], input="", text=True)  # don't leave a secret on the clipboard
        c = DhanClient(d["client_id"], d["access_token"], d["pin"], d["totp_secret"])
        try:
            c.profile()
        except Exception as exc:
            sys.exit(f"Dhan refused these keys: {exc}\nNothing was saved.")
        cfg.setdefault("dhan", {}).update(d, is_connected=True)
        cfg["broker"] = "dhan"
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)
        print("✓ Dhan keys work and are saved.\n")
    d = cfg.get("dhan") or {}
    c = DhanClient(d.get("client_id", ""), d.get("access_token", ""), d.get("pin", ""), d.get("totp_secret", ""))
    if not c.configured:
        sys.exit("No Dhan keys saved yet. Run without --check first.")
    sys.exit(0 if check(c) else 1)


if __name__ == "__main__":
    main()
