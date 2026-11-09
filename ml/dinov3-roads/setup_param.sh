#!/bin/bash
# One-time setup on the PARAM Rudra LOGIN node. Not an sbatch script - run it
# directly, because it needs the internet and compute nodes have none.
#
#   bash setup_param.sh            # the DINOv3 environment
#   bash setup_param.sh acpv       # the ACPV-Net environment as well
#
# Creates the conda env, installs torch for A100 (sm_80), and caches the
# DINOv3 weights under /scratch so the training job can run offline.

set -euo pipefail
WHICH="${1:-dinov3}"
SCRATCH="/scratch/$USER"
mkdir -p "$SCRATCH/hf" "$SCRATCH/torch" "$SCRATCH/data" "$SCRATCH/runs" "$SCRATCH/infer"

module load mldl/Miniconda
# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"

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
echo "  python preflight.py --data $SCRATCH/ongole_roads --backbone vit_7b_patch16_dinov3.sat493m --batch 8"
