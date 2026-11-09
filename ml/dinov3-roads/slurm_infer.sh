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

module purge
module load mldl/Miniconda
# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "$ENVNAME"

export HF_HOME="${HF_HOME:-/scratch/$USER/hf}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

echo "ckpt  : $CKPT"
echo "image : $IMAGE"
echo "out   : $OUT"

python infer_area.py \
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
