#!/usr/bin/env bash
# Building Height Server - start on the DSM machine (macOS / Linux).
#
#   ./start.sh                            use the built-in folder paths
#   ./start.sh /data/dsm /data/dtm        point it somewhere else
set -euo pipefail
cd "$(dirname "$0")"

[ $# -ge 1 ] && export HS_DSM_DIR="$1"
[ $# -ge 2 ] && export HS_DTM_DIR="$2"
export HS_PORT="${HS_PORT:-8900}"

if [ ! -x ".venv/bin/python" ]; then
  echo "[*] First run - creating .venv and installing dependencies."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

echo
echo "[*] Checking the elevation data before serving..."
.venv/bin/python selfcheck.py

echo
echo "[*] Starting Building Height Server on port $HS_PORT"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$HS_PORT"
