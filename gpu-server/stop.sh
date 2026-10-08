#!/usr/bin/env bash
cd "$(dirname "$0")"
# Only kill processes that really are ours: after a crash, a saved PID can be reused by another process.
ours() { [ -r "/proc/$1/cmdline" ] && tr '\0' ' ' < "/proc/$1/cmdline" | grep -qE 'uvicorn|run\.sh|serve\.sh|cloudflared'; }
# Tell the webapp the GPU server is going away (it falls back to Hugging Face at once).
if [ -f data/tunnel.pid ] && [ -x .venv/bin/python ]; then .venv/bin/python publish_url.py --clear || true; fi
touch data/stop.flag                       # the supervisor must not restart the API
for f in data/serve.pid data/api.pid data/tunnel.pid; do
  if [ -f "$f" ]; then
    pid=$(cat "$f")
    if [ "$pid" != "$$" ] && ours "$pid"; then pkill -P "$pid" 2>/dev/null || true; kill "$pid" 2>/dev/null || true; echo "stopped $f ($pid)"; fi
    rm -f "$f"
  fi
done
