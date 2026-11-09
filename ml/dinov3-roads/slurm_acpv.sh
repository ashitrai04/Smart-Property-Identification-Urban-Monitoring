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
# The manual (p76, p80) says `module load mldl/Miniconda`. On this system Lmod
# answers "The following module(s) are unknown: mldl/Miniconda" - the manual is
# out of date, or that module sits behind a hierarchy. So look for one rather
# than hardcode a name, and fall back to the conda already active in the login
# shell, which is what the (base) in the prompt means.
#
# CONDA_BASE is captured BEFORE `module purge`: if conda did come from a
# module, purging would take it away too.
CONDA_BASE="$(conda info --base 2>/dev/null || true)"
module purge 2>/dev/null || true
for _m in mldl/Miniconda mldl/miniconda mldl/Anaconda miniconda Miniconda           anaconda Anaconda conda python/conda; do
    if module load "$_m" 2>/dev/null; then
        echo "conda module: $_m"
        CONDA_BASE="$(conda info --base 2>/dev/null || echo "$CONDA_BASE")"
        break
    fi
done
if [ -z "$CONDA_BASE" ] || [ ! -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
    echo "FATAL: no conda found. Run 'module avail' and send me the list." >&2
    exit 1
fi
echo "conda base  : $CONDA_BASE"
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
