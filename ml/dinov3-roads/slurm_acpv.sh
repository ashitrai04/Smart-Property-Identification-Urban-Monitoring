#!/bin/bash -x
# Fine-tune ACPV-Net on the Ongole chips, on PARAM Rudra.
#
# ACPV-Net trains from its own repo, not from train_roads.py: it is a
# multi-class polygonal vectoriser with its own config system, dataloader and
# PSLG reconstruction. This wrapper sets the environment and hands over.
#
#   sbatch --export=ALL,REPO=/scratch/$USER/ACPV-Net slurm_acpv.sh
#
# Before the first run, on the LOGIN node:
#   1. bash setup_param.sh acpv
#   2. clone the repo and download its three assets (see README, "ACPV-Net")
#   3. register the Ongole dataset in topomapper/config/paths_catalog.py
#   4. patch csm_triton.py if triton did not install (see README)

#SBATCH --job-name=acpv-roads
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-task=32
#SBATCH --time=48:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail
mkdir -p logs

REPO="${REPO:-/scratch/$USER/ACPV-Net}"
CONFIG="${CONFIG:-config-files/ongole512_train.yaml}"
ENVNAME="${ENVNAME:-acpvnet}"

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

cd "$REPO"
export PYTHONPATH="$REPO:${PYTHONPATH:-}"
export HF_HOME="${HF_HOME:-/scratch/$USER/hf}"
export HF_HUB_OFFLINE=1

echo "node   : $(hostname)"
echo "gpus   : $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | paste -sd'; ')"
echo "repo   : $REPO"
echo "config : $CONFIG"

# Fail fast on the three assets the repo needs but does not ship.
for f in models/first_stage_models/kl-f4/model.ckpt \
         pretrained/vssm_small_0229_ckpt_epoch_222.pth ; do
  [ -f "$f" ] || { echo "MISSING: $REPO/$f  (see README, 'ACPV-Net assets')"; exit 1; }
done
python -c "import torch, timm, mmcv, cv2, yaml; from mmcv.cnn import ConvModule; \
print('imports ok, torch', torch.__version__, 'cuda', torch.cuda.is_available())"
python -c "
try:
    import selective_scan_cuda_oflex; print('selective_scan CUDA kernel: present')
except ImportError:
    print('selective_scan CUDA kernel: absent -> pure-torch scan (slower, correct)')"

python scripts/train_iter.py --config-file "$CONFIG"

echo "done"
