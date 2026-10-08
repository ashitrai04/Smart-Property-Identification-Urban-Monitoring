#!/usr/bin/env bash
# Unattended helper — runs in the background next to the API (start it with nohup).
#  * Tunnel watchdog: if the public URL stops answering, start a new tunnel and
#    publish the new URL (the webapp and send_to_gpu.py find it by themselves).
#  * Waits for data/raw/$AUTO_FILE to finish uploading, then imports it and queues
#    segmentation + road-gated pothole detection (ingest_now.py).
# Stops when data/stop.flag exists (stop.sh) or another autopilot replaces it.
cd "$(dirname "$0")"
AUTO_FILE="${AUTO_FILE:-guntur_drone.tif}"
AUTO_NAME="${AUTO_NAME:-Guntur drone}"
AUTO_DISTRICT="${AUTO_DISTRICT:-guntur}"
echo $$ > data/autopilot.pid
log() { echo "[autopilot] $(date '+%F %T') $*"; }
fails=0
log "started — waiting for $AUTO_FILE, watching the tunnel"

while [ "$(cat data/autopilot.pid 2>/dev/null)" = "$$" ] && [ ! -f data/stop.flag ]; do
  # 1. tunnel watchdog
  url=$(cat data/public_url.txt 2>/dev/null)
  if [ -n "$url" ] && curl -sf -m 20 "$url/api/health" >/dev/null; then
    fails=0
  else
    fails=$((fails + 1))
    if [ "$fails" -ge 3 ]; then
      log "public URL not answering ($url) — new tunnel"
      [ -f data/tunnel.pid ] && kill "$(cat data/tunnel.pid)" 2>/dev/null
      bash tunnel.sh
      fails=0
    fi
  fi

  # 2. import the upload once it is complete (the .part file is renamed when done)
  if [ -f "data/raw/$AUTO_FILE" ] && [ ! -f "data/raw/$AUTO_FILE.part" ] && [ ! -f "data/.auto_$AUTO_FILE.done" ]; then
    log "$AUTO_FILE uploaded — importing and queueing analysis"
    MPLBACKEND=Agg .venv/bin/python ingest_now.py "$AUTO_FILE" --name "$AUTO_NAME" --district "$AUTO_DISTRICT" \
      && touch "data/.auto_$AUTO_FILE.done" || log "import failed — see above (will not retry)"
    touch "data/.auto_$AUTO_FILE.done"
  fi
  sleep 60
done
log "stopped"
