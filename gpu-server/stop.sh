#!/usr/bin/env bash
cd "$(dirname "$0")"
# Tell the webapp the GPU server is going away (it falls back to Hugging Face at once).
if [ -f data/tunnel.pid ] && [ -x .venv/bin/python ]; then .venv/bin/python publish_url.py --clear || true; fi
for f in data/api.pid data/tunnel.pid; do
  if [ -f "$f" ]; then
    pid=$(cat "$f"); pkill -P "$pid" 2>/dev/null || true; kill "$pid" 2>/dev/null || true
    rm -f "$f"; echo "stopped $f ($pid)"
  fi
done
