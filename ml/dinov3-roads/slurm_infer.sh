#!/bin/bash -x
# Inference over a 1 km2 area with a trained checkpoint, on PARAM Rudra.
#
#   sbatch --export=ALL,CKPT=/scratch/$USER/runs/<run>/best.pth slurm_infer.sh
#
# One GPU and one hour is plenty: a 1 km2 area at 20 cm is 5000 x 5000 px,
# which is about 150 tiles of 512 with overlap.

# Validate first:  sbatch --test-only slurm_infer.sh
#SBATCH --job-name=roads-infer
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail
mkdir -p logs

CODE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # see slurm_train.sh
CKPT="${CKPT:?set CKPT=/scratch/$USER/runs/<run>/best.pth}"
# The 45 GB mosaic is NOT on PARAM - only the pre-cut 1 km2 clip is.
# This used to default to the mosaic, which does not exist here, so the
# job would have died on its first line after waiting in the queue.
IMAGE="${IMAGE:-/scratch/$USER/data/ongole_1km.tif}"
LABELS="${LABELS:-/scratch/$USER/data/centre_part_Rd.shp}"
OUT="${OUT:-/scratch/$USER/infer/$(basename "$(dirname "$CKPT")")-1km}"
# 0 = the whole image. The clip is 33333 x 33333 at 0.03 m, exactly
# 1000 m a side, so --area-km2 1.0 also lands on it - but to the pixel,
# and a rounding change either way would crop or overrun it.
AREA="${AREA:-0}"
GSD="${GSD:-0.20}"
ENVNAME="${ENVNAME:-dinov3}"

# ---------- find conda, whatever this cluster calls it ----------
# Three things are true here and each one breaks the obvious approach:
#
#   1. `module load mldl/Miniconda` - straight from the manual, p76 and p80 -
#      fails on this system: "The following module(s) are unknown".
#   2. conda IS already active at login; the prompt says (base).
#   3. but `conda` is a shell FUNCTION, not a program. `which conda` prints a
#      function body. Functions are not exported, and a script run as
#      `bash setup_param.sh` is a new non-interactive shell that never sources
#      ~/.bashrc - so inside here, `conda` does not exist at all.
#
# What does survive is the environment: conda's init exports CONDA_EXE (the
# real binary) and CONDA_PREFIX (the active environment). Those cross the
# process boundary, and SLURM exports the submitting environment by default,
# so they reach the compute nodes too. So: find the binary, not the function.
CONDA_BASE=""
_try() { [ -n "${1:-}" ] && [ -f "$1/etc/profile.d/conda.sh" ]; }

# 1. CONDA_EXE - the most reliable, set by conda's own init.
if [ -n "${CONDA_EXE:-}" ] && [ -x "${CONDA_EXE:-}" ]; then
    _b="$("$CONDA_EXE" info --base 2>/dev/null || true)"
    _try "$_b" && CONDA_BASE="$_b"
fi
# 2. CONDA_PREFIX - the active env. If it is an env rather than the base
#    install, it sits at <base>/envs/<name>, so strip that.
if [ -z "$CONDA_BASE" ] && [ -n "${CONDA_PREFIX:-}" ]; then
    _try "$CONDA_PREFIX" && CONDA_BASE="$CONDA_PREFIX"
    if [ -z "$CONDA_BASE" ]; then
        _b="${CONDA_PREFIX%/envs/*}"
        _try "$_b" && CONDA_BASE="$_b"
    fi
fi
# 3. a real conda on PATH (not the function - this is a new shell).
if [ -z "$CONDA_BASE" ] && command -v conda >/dev/null 2>&1; then
    _b="$(conda info --base 2>/dev/null || true)"
    _try "$_b" && CONDA_BASE="$_b"
fi
# 4. only now bother with modules, and try several names.
if [ -z "$CONDA_BASE" ]; then
    for _m in mldl/Miniconda mldl/miniconda mldl/Anaconda miniconda Miniconda               anaconda Anaconda conda python/conda; do
        module load "$_m" 2>/dev/null || continue
        echo "conda module: $_m"
        _b="$(conda info --base 2>/dev/null || true)"
        _try "$_b" && { CONDA_BASE="$_b"; break; }
    done
fi
# 5. last resort: the usual places.
if [ -z "$CONDA_BASE" ]; then
    for _b in /home/apps/miniconda3 /home/apps/anaconda3 /opt/miniconda3               /opt/anaconda3 "$HOME/miniconda3" "$HOME/anaconda3"; do
        _try "$_b" && { CONDA_BASE="$_b"; break; }
    done
fi
if [ -z "$CONDA_BASE" ]; then
    echo "FATAL: cannot find a conda installation." >&2
    echo "  CONDA_EXE=${CONDA_EXE:-<unset>}" >&2
    echo "  CONDA_PREFIX=${CONDA_PREFIX:-<unset>}" >&2
    echo "  Run 'module avail' and 'echo \$CONDA_EXE' and send me both." >&2
    exit 1
fi
echo "conda base  : $CONDA_BASE"
# Sourcing this is what defines `conda` in THIS shell - without it,
# `conda activate` fails with "run 'conda init' before 'conda activate'".
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$ENVNAME"

export HF_HOME="${HF_HOME:-/scratch/$USER/hf}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

echo "ckpt  : $CKPT"
echo "image : $IMAGE"
echo "out   : $OUT"

python "$CODE/infer_area.py" \
    --image "$IMAGE" \
    --ckpt  "$CKPT" \
    --out   "$OUT" \
    --area-km2 "$AREA" \
    --gsd "$GSD" \
    --threshold 0.45 \
    --regularize smooth \
    ${LABELS:+--labels "$LABELS"}

echo "done -> $OUT"
ls -la "$OUT"
