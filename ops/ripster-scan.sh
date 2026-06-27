#!/usr/bin/env bash
# ripster-scan.sh — HOST-side cron wrapper for the fusion watchlist scan.
# Runs the scan INSIDE the running market-terminal container, with an
# overlap lock and logging. Install on the VPS host (not in the image).
#
#   sudo install -m 755 ripster-scan.sh /opt/zyvora/ops/ripster-scan.sh
#
# Env overrides: RIPSTER_CONTAINER (default market-terminal), RIPSTER_LOG.
set -euo pipefail

CONTAINER="${RIPSTER_CONTAINER:-market-terminal}"
LOG="${RIPSTER_LOG:-/var/log/ripster-scan.log}"
LOCK="/tmp/ripster-scan.lock"
ts() { date -u +%FT%TZ; }

# prevent overlapping runs (a scan can take longer than the 15-min tick)
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(ts) skip: previous run still active" >>"$LOG"
  exit 0
fi

echo "$(ts) ripster-scan start" >>"$LOG"
if docker exec "$CONTAINER" python3 /app/ripster_watchlist_scan.py --all-users >>"$LOG" 2>&1; then
  echo "$(ts) ripster-scan done" >>"$LOG"
else
  echo "$(ts) ripster-scan exited non-zero" >>"$LOG"
fi
