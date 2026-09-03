#!/bin/bash
# Starts everything needed for an unattended review sweep:
#   collector (serves the queue + ingests), caffeinate (stops the Mac sleeping),
#   and a progress watcher. Ctrl-C stops all three.
cd "$(dirname "$0")" || exit 1

pkill -f "caffeinate -dimsu -w" 2>/dev/null
pkill -f "watch_sweep.py" 2>/dev/null

python3 scrapers/collector.py &
COLLECTOR=$!
echo "collector       pid $COLLECTOR"

caffeinate -dimsu -w $COLLECTOR &
echo "caffeinate      pid $! (exits when the collector does)"

python3 scrapers/watch_sweep.py 300 $COLLECTOR &
echo "progress watch  pid $!  -> logs/sweep.log"

echo
echo "Now: reload the extension at chrome://extensions, open a Dan Murphy's tab,"
echo "     then popup -> Start review sweep."
echo "Ctrl-C here stops everything."

trap 'kill $COLLECTOR 2>/dev/null; exit 0' INT TERM
wait $COLLECTOR
