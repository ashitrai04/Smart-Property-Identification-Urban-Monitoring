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

module load mldl/Miniconda
# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"
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
