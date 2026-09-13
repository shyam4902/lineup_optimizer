#!/usr/bin/env bash
# Isolated preview for the V1 correction pass on 127.0.0.1:5097.
# Reuses the packet-4 isolated roster/snapshot copy. The user's instances on other
# ports and the real ~/Library/Application Support/GameBlazers data are untouched.
set -euo pipefail
ROOT=/tmp/gb_correction_preview
SEED=/tmp/gb_p4_preview
APP=/Users/shyampatel/Desktop/GB/lineup_optimizer
SESSION=gbcorr
PASSWORD=correctionpass

mkdir -p "$ROOT/data/raw" "$ROOT/data/snapshots" "$ROOT/data/saved_plans"
if [ ! -f "$ROOT/data/raw/My_roster.csv" ]; then
  cp "$SEED/data/raw/My_roster.csv" "$ROOT/data/raw/My_roster.csv"
fi
if [ ! -f "$ROOT/data/snapshots/latest_snapshot.json" ]; then
  cp "$SEED/data/snapshots/latest_snapshot.json" "$ROOT/data/snapshots/latest_snapshot.json"
fi
printf '%s' "$PASSWORD" > "$ROOT/password.txt"

screen -S "$SESSION" -X quit 2>/dev/null || true
OLD=$(lsof -nP -iTCP:5097 -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$OLD" ]; then
  ps -E -p "$OLD" 2>/dev/null | grep -q "PLANNER_INSTANCE_ROOT=$ROOT" || { echo "refusing to stop pid $OLD: not the correction instance"; exit 1; }
  kill "$OLD"
  sleep 2
fi

screen -dmS "$SESSION" bash -c "cd $APP && PLANNER_INSTANCE_ROOT=$ROOT PLANNER_PASSWORD_FILE=$ROOT/password.txt PLANNER_USERNAME=friend PORT=5097 exec .venv/bin/python app.py"
for _ in $(seq 1 20); do
  if curl -sf -u "friend:$PASSWORD" http://127.0.0.1:5097/api/planner/state >/dev/null; then break; fi
  sleep 0.5
done
curl -s -u "friend:$PASSWORD" http://127.0.0.1:5097/api/planner/state | python3 -c "import json,sys; d=json.load(sys.stdin); print('instance root /tmp/gb_correction_preview on port 5097 :: roster', d['roster_count'], 'cards :: active plan', bool(d['active_plan']))"
