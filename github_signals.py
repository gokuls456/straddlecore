"""
GitHub Actions runner for the strategy signal scanner (Telegram alerts only, no orders).

    python github_signals.py --until 12:15

Runs the scanner every 30 seconds until the given IST time, then exits. Settings come
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
import time
from datetime import datetime, timedelta
from datetime import time as dtime

from engine import telegram_notifier as tg
from engine.signal_scanner import ALL_STRATEGIES, SignalScanner, TICK_SECONDS, now_ist

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "docs", "data")
SETTINGS_FILE = os.path.join(ROOT, "signals_config.json")
COMMIT_EVERY = timedelta(minutes=10)

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


def write_status(scanner: SignalScanner, now: datetime):
    status = scanner.get_status()
    status["totals"] = scanner.totals(now.date())
    status["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S IST")
    with open(os.path.join(DATA_DIR, "status.json"), "w") as f:
        json.dump(status, f, indent=1, default=str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", default="15:40", help="IST time to stop, HH:MM")
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

    os.makedirs(DATA_DIR, exist_ok=True)
    scanner = SignalScanner(
        sender=send,
        state_file=os.path.join(DATA_DIR, "state.json"),
        history_file=os.path.join(DATA_DIR, "history.json"),
        config_loader=load_settings,
    )

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
        if scanner.state.get("no_data_noted"):
            log.info("No NIFTY candles today (market holiday?). Exiting.")
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


if __name__ == "__main__":
    main()
