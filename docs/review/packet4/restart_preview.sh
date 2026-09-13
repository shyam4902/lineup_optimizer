#!/usr/bin/env bash
# Restart the isolated packet-4 preview on 127.0.0.1:5098.
# Only the listener on port 5098 is stopped; the user's previews (5013-5016) are untouched.
set -euo pipefail
ROOT=/tmp/gb_p4_preview
APP=/Users/shyampatel/Desktop/GB/lineup_optimizer
SESSION=gbp4

screen -S "$SESSION" -X quit 2>/dev/null || true
OLD=$(lsof -nP -iTCP:5098 -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$OLD" ]; then
  ps -E -p "$OLD" 2>/dev/null | grep -q "PLANNER_INSTANCE_ROOT=$ROOT" || { echo "refusing to stop pid $OLD: not the packet 4 instance"; exit 1; }
  kill "$OLD"
  sleep 2
fi

screen -dmS "$SESSION" bash -c "cd $APP && PLANNER_INSTANCE_ROOT=$ROOT PLANNER_PASSWORD_FILE=$ROOT/password.txt PLANNER_USERNAME=friend NFL_DASHBOARD_BOARD_PATH=/Users/shyampatel/Desktop/NFL_Main/nfldashboard/props-board.json PORT=5098 exec .venv/bin/python app.py"
for _ in $(seq 1 20); do
  if curl -sf -u friend:packet4pass http://127.0.0.1:5098/api/planner/state >/dev/null; then break; fi
  sleep 0.5
done
curl -s -u friend:packet4pass http://127.0.0.1:5098/api/planner/state | python3 -c "import json,sys; d=json.load(sys.stdin); print('instance root /tmp/gb_p4_preview on port 5098 :: roster', d['roster_count'], 'cards :: active plan', bool(d['active_plan']))"
