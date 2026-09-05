#!/bin/sh
# Supervisor for the BWS review sweep, for unattended runs.
#
# The lane exits 1 when it gives up after FAILURE_LIMIT consecutive product
# failures, which is what a dropped VPN looks like: BWS is behind Cloudflare and
# answers 403 from a non-VPN IP. Restart it a bounded number of times so a flaky
# connection does not waste an unattended run, but stop rather than loop forever
# on a block that is never going to clear.
#
# Progress is checkpointed by the lane itself, so each attempt resumes where the
# last one stopped and already-collected reviews are never re-fetched.
#
#   caffeinate -dimsu ./run_bws_reviews.sh
#
# caffeinate holds the machine awake for exactly as long as this script runs and
# releases it the moment the sweep finishes.
cd "$(dirname "$0")" || exit 1

MAX=${MAX:-8}
DELAY=${DELAY:-0.6}
WAIT=${WAIT:-120}
LOG=logs/reviews_bws.log

i=1
while [ "$i" -le "$MAX" ]; do
  echo "=== attempt $i/$MAX started $(date '+%H:%M:%S') ===" >>"$LOG"
  if python3 -u scrapers/reviews_api_lane.py --source bws --delay "$DELAY" >>"$LOG" 2>&1; then
    echo "=== finished cleanly $(date '+%H:%M:%S') ===" >>"$LOG"
    exit 0
  fi
  echo "=== attempt $i stopped early; waiting ${WAIT}s for the connection ===" >>"$LOG"
  sleep "$WAIT"
  i=$((i + 1))
done

echo "=== gave up after $MAX attempts $(date '+%H:%M:%S') ===" >>"$LOG"
exit 1
