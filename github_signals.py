"""
GitHub Actions runner for the strategy signal scanner (Telegram alerts only, no orders).

    python github_signals.py --auto          (what the workflow runs)
    python github_signals.py --until 12:15

Runs the scanner every 30 seconds until the given IST time, then exits. With --auto it works
out from the clock what to do: scan the morning (until 12:15) or the afternoon (until 15:40)
on weekdays, otherwise sleep until the next session (at most MAX_SLEEP). The workflow then
starts the next run itself, so the runs form an unbroken chain that does not depend on
GitHub's unreliable cron. Settings come
from signals_config.json; the Telegram bot token and chat ID come from the
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment variables (GitHub secrets).

State, P&L history and the status page data live in docs/data/ and are committed back
to the repository, so the afternoon run continues the morning run and the GitHub Pages
status page (docs/index.html) can show them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from datetime import time as dtime

from engine import telegram_notifier as tg
from engine.signal_scanner import ALL_STRATEGIES, SignalScanner, TICK_SECONDS, now_ist

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "docs", "data")
SETTINGS_FILE = os.path.join(ROOT, "signals_config.json")
COMMIT_EVERY = timedelta(minutes=10)
FIRST_START = dtime(8, 50)       # scanner must be running before the 10:00 Iron Butterfly entry
MORNING_END = dtime(12, 15)      # a GitHub job is capped at 6 h, so the day is two runs
DAY_END = dtime(15, 40)
MAX_SLEEP = timedelta(hours=5, minutes=15)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("github_signals")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
    except Exception as exc:
        log.warning("Could not read signals_config.json (%s); using defaults.", exc)
        s = {}
    return {
        "signals": {"enabled": s.get("enabled", True), "strategies": s.get("strategies", ALL_STRATEGIES)},
        "allocated_capital": s.get("capital", 40000),
        # Optional live option prices (GitHub secrets); without them prices are estimates.
        "indstocks": {
            "client_id": os.environ.get("INDSTOCKS_CLIENT_ID", ""),
            "mpin": os.environ.get("INDSTOCKS_MPIN", ""),
            "totp_secret": os.environ.get("INDSTOCKS_TOTP_SECRET", ""),
            "access_token": os.environ.get("INDSTOCKS_ACCESS_TOKEN", ""),
        },
        "groww": {
            "api_key": os.environ.get("GROWW_API_KEY", ""),
            "api_secret": os.environ.get("GROWW_API_SECRET", ""),
            "totp_secret": os.environ.get("GROWW_TOTP_SECRET", ""),
        },
    }


def git(*args: str) -> bool:
    res = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if res.returncode:
        log.warning("git %s failed: %s", " ".join(args), res.stderr.strip())
    return res.returncode == 0


def commit_data(message: str):
    git("add", "docs/data")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-m", message)
    # Pull first so settings edited from the phone mid-day are kept (and picked up).
    if not (git("pull", "--rebase", "--autostash") and git("push")):
        log.warning("Push failed; will retry at the next commit.")


def diagnose_groww(scanner: SignalScanner):
    """Log which Groww data calls this key may use (results only, never credentials)."""
    q = scanner.quotes()
    if not q or not q._login():
        log.info("GROWW DIAG: login failed: %s", q.last_error if q else "no keys")
        return
    from engine.signal_scanner import nifty_weekly_expiry
    c, exp = q.client, nifty_weekly_expiry(now_ist().date()).isoformat()

    def attempt(name, fn):
        try:
            out = fn()
            log.info("GROWW DIAG %-14s OK   %s", name, json.dumps(out, default=str)[:300])
            return out
        except Exception as exc:
            log.info("GROWW DIAG %-14s FAIL %s: %s", name, type(exc).__name__, str(exc)[:200])

    attempt("profile", lambda: {k: v for k, v in (c.get_user_profile(timeout=10) or {}).items() if k in ("active_segments", "ddpi_enabled")})
    attempt("index ltp", lambda: c.get_ltp(exchange_trading_symbols=("NSE_NIFTY",), segment="CASH", timeout=10))
    attempt("expiries", lambda: c.get_expiries(exchange="NSE", underlying_symbol="NIFTY", timeout=10))
    attempt("option chain", lambda: c.get_option_chain("NSE", "NIFTY", exp, timeout=12))
    inst = attempt("instruments", lambda: len(c.get_all_instruments()))
    if inst:
        df = c.get_all_instruments()
        opt = df[(df.get("underlying_symbol") == "NIFTY") & (df.get("segment") == "FNO")] if "underlying_symbol" in df else df.head(0)
        log.info("GROWW DIAG columns %s", list(df.columns)[:20])
        if not opt.empty:
            row = opt.iloc[0]
            sym = row.get("trading_symbol")
            log.info("GROWW DIAG sample option row %s", {k: row.get(k) for k in ("trading_symbol", "expiry_date", "strike_price", "instrument_type")})
            attempt("option ltp", lambda: c.get_ltp(exchange_trading_symbols=(f"NSE_{sym}",), segment="FNO", timeout=10))
            attempt("option quote", lambda: {k: v for k, v in (c.get_quote(sym, "NSE", "FNO", timeout=10) or {}).items() if k in ("last_price", "ltp", "ohlc")})


def price_check(scanner: SignalScanner) -> str:
    """One line saying whether live Groww option prices work from here."""
    q = scanner.quotes()
    if not q:
        return "💲 Prices: estimated (no price API keys added)"
    now = now_ist()
    spot = 0.0
    try:
        candles, _, _ = scanner.data_source(now)
        spot = float(candles["close"].iloc[-1]) if not candles.empty else 0.0
    except Exception:
        pass
    if not spot:
        try:
            import yfinance as yf
            spot = float(yf.Ticker("^NSEI").history(period="5d")["Close"].iloc[-1])
        except Exception:
            spot = 23000.0
    from engine.straddle_engine import get_atm_strike
    atm = get_atm_strike(spot)
    px = scanner._live_price(now, atm, "CE")
    if px:
        return f"💲 Prices: {q.name} live ✅ (NIFTY {atm} CE ₹{px:,.2f})"
    log.warning("%s quote check failed: %s", q.name, q.last_error)
    return f"💲 Prices: estimated ⚠️ {q.name}: {tg.esc(q.last_error or 'no price returned')}"


def write_status(scanner: SignalScanner, now: datetime):
    status = scanner.get_status()
    status["totals"] = scanner.totals(now.date())
    status["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S IST")
    with open(os.path.join(DATA_DIR, "status.json"), "w") as f:
        json.dump(status, f, indent=1, default=str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", default="15:40", help="IST time to stop, HH:MM")
    parser.add_argument("--auto", action="store_true", help="decide from the clock: scan or sleep until the next session")
    parser.add_argument("--test", action="store_true", help="send one test alert and exit (works any time)")
    args = parser.parse_args()
    until = dtime.fromisoformat(args.until)

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: alerts will only be logged.")

    def send(text: str):
        if not (token and chat):
            log.info("ALERT (not sent):\n%s", text)
            return
        ok, err = tg.send_message(token, chat, text)
        if not ok:
            log.warning("Telegram send failed: %s", err)

    if args.test:
        if not (token and chat):
            sys.exit("Test failed: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID under Settings → Secrets and variables → Actions.")
        scanner = SignalScanner(sender=send, config_loader=load_settings)
        if scanner.quotes() and scanner.quotes().name == "Groww":
            diagnose_groww(scanner)
        sample = scanner.sample_text()
        ok, err = tg.send_message(token, chat, "✅ <b>GitHub Actions connected</b>\n" + price_check(scanner)
                                  + "\nWeekday scans will send alerts here. Sample format:\n\n" + sample)
        if not ok:
            sys.exit(f"Test failed: Telegram said: {err}")
        log.info("Test alert sent.")
        return

    if args.auto:
        auto(send)
    else:
        scan(send, until)


def scan_until(now: datetime):
    """IST stop time for a scan starting now, or None outside market hours."""
    if now.weekday() >= 5:
        return None
    if FIRST_START <= now.time() < MORNING_END:
        return MORNING_END
    if MORNING_END <= now.time() < DAY_END:
        return DAY_END
    return None


def next_session(now: datetime, skip_today: bool = False) -> datetime:
    d = now.date() + timedelta(days=1 if skip_today else 0)
    while True:
        start = datetime.combine(d, FIRST_START)
        if d.weekday() < 5 and start > now:
            return start
        d += timedelta(days=1)


def no_market_today(now: datetime) -> bool:
    """True when an earlier run today already found no NIFTY candles (holiday)."""
    try:
        with open(os.path.join(DATA_DIR, "state.json")) as f:
            st = json.load(f)
    except Exception:
        return False
    return st.get("date") == now.date().isoformat() and bool(st.get("no_data_noted"))


def auto(send):
    """One link of the chain: scan this half of the day, or sleep. Never both, so a run
    stays well inside GitHub's 6-hour job limit; the next run picks up from there."""
    now = now_ist()
    until = scan_until(now)
    holiday = bool(until) and no_market_today(now)
    if until and not holiday:
        scan(send, until)
        return
    wake = next_session(now, skip_today=holiday)
    pause = min(wake - now, MAX_SLEEP)
    log.info("Market closed. Sleeping %s (next session %s); the next run is started after.", pause, wake)
    time.sleep(max(0, pause.total_seconds()))


def scan(send, until: dtime) -> bool:
    """Scan until `until` (IST). Returns True if there is no market today (holiday)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    scanner = SignalScanner(
        sender=send,
        state_file=os.path.join(DATA_DIR, "state.json"),
        history_file=os.path.join(DATA_DIR, "history.json"),
        config_loader=load_settings,
    )

    holiday = False
    last_commit = now_ist()
    last_snapshot = None
    while True:
        now = now_ist()
        if now.time() >= until:
            break
        try:
            scanner.tick(now)
        except Exception as exc:
            log.warning("Scan failed: %s", exc)
        write_status(scanner, now)

        # Holiday / no data by 9:45: nothing will happen today, so stop early.
        if scanner.state.get("no_data_noted") and scanner.state.get("date") == now.date().isoformat():
            log.info("No NIFTY candles today (market holiday?). Exiting.")
            holiday = True
            break

        # Commit right away when a strategy changes state (signal / exit), else every 10 minutes for live P&L.
        snapshot = json.dumps([r["status"] for r in scanner.get_status()["strategies"]])
        if (last_snapshot is not None and snapshot != last_snapshot) or now - last_commit >= COMMIT_EVERY:
            commit_data(f"Signals update {now.strftime('%d %b %H:%M')}")
            last_commit = now
        last_snapshot = snapshot
        time.sleep(TICK_SECONDS)

    write_status(scanner, now_ist())
    commit_data(f"Signals update {now_ist().strftime('%d %b %H:%M')} (run end)")
    log.info("Done.")
    return holiday


if __name__ == "__main__":
    main()
