#!/usr/bin/env bash
# Start the API in the foreground (start.sh runs this in the background).
set -euo pipefail
cd "$(dirname "$0")"
set -a; [ -f config.env ] && . ./config.env; set +a
export HF_HOME="${HF_HOME:-$PWD/weights/hf}" YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-$PWD/data/.ultralytics}"
# Launched from a Jupyter notebook, the process inherits MPLBACKEND=module://matplotlib_inline…,
# which does not exist in this venv and crashes matplotlib on import. Always headless here.
export MPLBACKEND=Agg
# Shared GPU: return freed blocks and avoid fragmentation when memory is tight.
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
PORT="${PORT:-8800}"
MIN_FREE_MB="${MIN_FREE_MB:-2500}"

# Both A100s are shared: pick the one with the most free memory right now.
# If neither has MIN_FREE_MB free, run on CPU (256 cores) instead of failing.
if [ -z "${SEG_DEVICE:-}" ] && command -v nvidia-smi >/dev/null 2>&1; then
  best=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits | sort -t, -k2 -nr | head -1)
  idx=$(echo "$best" | cut -d, -f1 | tr -d ' '); free=$(echo "$best" | cut -d, -f2 | tr -d ' ')
  if [ "${free:-0}" -ge "$MIN_FREE_MB" ]; then
    export CUDA_VISIBLE_DEVICES="$idx"
    echo "[run] GPU $idx selected (${free} MiB free)"
  else
    export CUDA_VISIBLE_DEVICES="" SEG_DEVICE=cpu
    echo "[run] no GPU has ${MIN_FREE_MB} MiB free (best: ${free} MiB) — running on CPU"
  fi
fi

exec .venv/bin/python -m uvicorn main:app --app-dir app --host 0.0.0.0 --port "$PORT" --workers 1 --timeout-keep-alive 75
