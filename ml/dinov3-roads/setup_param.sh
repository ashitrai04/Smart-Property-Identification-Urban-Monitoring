#!/bin/bash
# One-time setup on the PARAM Rudra LOGIN node. Not an sbatch script - run it
# directly, because it needs the internet and compute nodes have none.
#
#   bash setup_param.sh            # the DINOv3 environment (ViT-7B + ViT-L)
#   bash setup_param.sh acpv       # the ACPV-Net environment
#   bash setup_param.sh all        # both
#
# Creates the conda env, installs torch for A100 (sm_80), and caches the
# DINOv3 weights under /scratch so the training job can run offline.

set -euo pipefail
WHICH="${1:-dinov3}"
SCRATCH="/scratch/$USER"
mkdir -p "$SCRATCH/hf" "$SCRATCH/torch" "$SCRATCH/data" "$SCRATCH/runs" "$SCRATCH/infer"

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

# Put the environments on /scratch, not in /home/$USER/.conda.
#
# /home is 50 GB soft, 60 GB hard (manual p39) and these two environments are
# a torch+cu121 build each - roughly 10 GB apiece. Adding the weights cache
# would take it past the limit, and a quota breach has a 7-day grace period
# and then starts failing writes, which is a miserable way to lose a training
# run. /scratch is 200 GB.
#
# envs_dirs rather than `conda create -p`: the environments keep their plain
# names, so `conda activate dinov3` still works in the SLURM scripts and
# nothing downstream needs to know where they live.
mkdir -p "$SCRATCH/envs"
conda config --add envs_dirs "$SCRATCH/envs" 2>/dev/null || true
echo "environments will be created under $SCRATCH/envs"

if [ "$WHICH" = "dinov3" ] || [ "$WHICH" = "all" ]; then
  echo "=== dinov3 environment ==="
  conda create -y -n dinov3 python=3.10
  conda activate dinov3
  # cu121 wheels cover the A100 (sm_80) and are what torch 2.x ships against.
  pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu121
  pip install --no-cache-dir "timm>=1.0.19" rasterio geopandas shapely pyogrio \
      pillow numpy scipy scikit-image opencv-python-headless tqdm

  export HF_HOME="$SCRATCH/hf"
  echo "=== caching DINOv3 weights into $HF_HOME (login node has internet) ==="
  python prefetch_weights.py vit_large_patch16_dinov3.sat493m
  python prefetch_weights.py vit_7b_patch16_dinov3.sat493m     # 13.4 GB
  conda deactivate
fi

if [ "$WHICH" = "acpv" ] || [ "$WHICH" = "all" ]; then
  echo "=== acpvnet environment ==="
  conda create -y -n acpvnet python=3.10
  conda activate acpvnet
  pip install --no-cache-dir torch==2.1.0 torchvision==0.16.0 \
      --index-url https://download.pytorch.org/whl/cu121
  pip install --no-cache-dir "numpy==1.26.4" scipy scikit-image matplotlib pillow \
      pyyaml cython ninja packaging tqdm "opencv-python-headless==4.10.0.84" \
      imagecodecs "yacs==0.1.8" omegaconf einops "pytorch-lightning<2.0" \
      pycocotools shapely multiprocess timm fvcore iopath taming-transformers-rom1504 \
      rasterio geopandas pyogrio
  # mmcv's build needs torch already importable, so isolation has to be off.
  # Only mmcv.cnn.ConvModule is ever used, so the lite package is enough.
  pip install --no-cache-dir --no-build-isolation "mmcv==1.7.2"
  # triton has no wheel for some setups; the backbone falls back to a pure
  # torch scan when it is absent, so this is allowed to fail.
  pip install --no-cache-dir triton==2.1.0 || \
      echo "   triton unavailable - VMamba will use the pure-torch scan (slower, correct)"
  conda deactivate
fi

echo
echo "done. Environments:"
conda env list | grep -E "dinov3|acpvnet" || true
echo
echo "Next: upload the data, then run preflight inside a debug allocation:"
echo "  salloc -p gpu --gres=gpu:1 -t 00:30:00"
echo "  python preflight.py --data $SCRATCH/ongole_roads_v2 --backbone vit_7b_patch16_dinov3.sat493m --batch 8"
