#!/usr/bin/env bash
# Start the API (under a supervisor that restarts it if it crashes) and the public
# HTTPS tunnel, in the background; both survive closing the notebook.
#   bash start.sh          API + tunnel
#   bash start.sh --no-tunnel
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data/logs
bash stop.sh >/dev/null 2>&1 || true

nohup bash serve.sh >> data/logs/api.log 2>&1 &
echo "API starting under supervisor — log: data/logs/api.log"

if [ "${1:-}" != "--no-tunnel" ]; then
  bash tunnel.sh
fi

PORT=$(grep -E '^PORT=' config.env 2>/dev/null | cut -d= -f2 | cut -d' ' -f1); PORT="${PORT:-8800}"
for i in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && { echo "API is up on :$PORT"; break; }
  sleep 2
done
