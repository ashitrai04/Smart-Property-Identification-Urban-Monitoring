#!/usr/bin/env bash
# Supervisor: keeps the API running. If it exits or crashes, it is restarted in 3 s.
# Restart the API with new code by killing just the API process:  kill $(cat data/api.pid)
cd "$(dirname "$0")"
mkdir -p data/logs
echo $$ > data/serve.pid
rm -f data/stop.flag
while true; do
  bash run.sh &
  echo $! > data/api.pid
  wait $!
  code=$?
  [ -f data/stop.flag ] && break
  echo "[serve] $(date '+%F %T') API exited (code $code) — restarting in 3 s"
  sleep 3
done
