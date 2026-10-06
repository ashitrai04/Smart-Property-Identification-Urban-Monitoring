#!/usr/bin/env bash
# Start the API (and the public HTTPS tunnel) in the background; survives closing the notebook.
#   bash start.sh          API + tunnel
#   bash start.sh --no-tunnel
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data/logs
bash stop.sh >/dev/null 2>&1 || true

nohup bash run.sh > data/logs/api.log 2>&1 &
echo $! > data/api.pid
echo "API starting (pid $(cat data/api.pid)) — log: data/logs/api.log"

if [ "${1:-}" != "--no-tunnel" ]; then
  bash tunnel.sh
fi

PORT=$(grep -E '^PORT=' config.env 2>/dev/null | cut -d= -f2); PORT="${PORT:-8800}"
for i in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && { echo "API is up on :$PORT"; break; }
  sleep 2
done
