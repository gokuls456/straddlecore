#!/bin/bash
# Starts the GitHub "Strategy signals" run from this Mac, because GitHub's own cron often
# starts hours late or not at all. launchd calls this on weekdays (scripts/com.straddlecore.signals.plist);
# if the Mac was asleep at that time, launchd runs it on wake.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO="gokuls456/straddlecore"
NOW=$(TZ=Asia/Kolkata date +%H%M)

if [ "$NOW" -ge 0830 ] && [ "$NOW" -lt 1015 ]; then
  UNTIL="12:15"   # morning run; must start before 10:15 or the 10 AM Iron Butterfly entry is missed
elif [ "$NOW" -ge 1200 ] && [ "$NOW" -lt 1530 ]; then
  UNTIL="15:40"   # afternoon run; queues behind the morning run if that is still going
else
  echo "$(date) nothing to start at $NOW"; exit 0
fi

ACTIVE=$(gh run list -R "$REPO" -w signals.yml -L 5 --json status -q '[.[] | select(.status != "completed")] | length')
if [ "$UNTIL" = "12:15" ] && [ "${ACTIVE:-0}" -gt 0 ]; then
  echo "$(date) morning run already active"; exit 0
fi
gh workflow run signals.yml -R "$REPO" -f until="$UNTIL" && echo "$(date) dispatched until $UNTIL"
