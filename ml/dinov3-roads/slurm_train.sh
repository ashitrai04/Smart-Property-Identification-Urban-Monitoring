#!/bin/bash -x
# Train the road segmenter on PARAM Rudra (IIT Patna).
#
# Configuration taken from the PARAM Rudra user manual, not guessed:
#   partition "gpu"   8 nodes, 2 x NVIDIA A100 80 GB HBM2e per node
#   walltime          default 2 h, maximum 4 days; 8-node jobs up to 4 days
#   conda             module load mldl/Miniconda
#   filesystem        Lustre, /home/<user> and /scratch
#
# The scheduler uses backfill, so an honest -t gets you started sooner: a job
# that asks for 4 days when it needs 6 hours waits behind everything.
# preflight.py prints the walltime to ask for.
#
#   sbatch slurm_train.sh                                              # ViT-L
#   sbatch --export=ALL,BACKBONE=vit_7b_patch16_dinov3.sat493m,BATCH=8 slurm_train.sh

#SBATCH --job-name=dinov3-roads
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-task=32
#SBATCH --time=24:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
## Uncomment only if you belong to more than one account; with a single
## allocation SLURM already picks it, and a wrong string fails the submission.
## #SBATCH --account=<your-slurm-account>

set -euo pipefail
mkdir -p logs

BACKBONE="${BACKBONE:-vit_large_patch16_dinov3.sat493m}"
DATA="${DATA:-/scratch/$USER/ongole_roads}"
OUT="${OUT:-/scratch/$USER/runs/$(date +%Y%m%d-%H%M)-${BACKBONE%%.*}}"
# Sized for an 80 GB A100, not a laptop card. The frozen 7B is 13.4 GB of bf16
# weights, so most of the card is free for activations. These are starting
# points only - preflight.py measures the real peak on the real hardware and
# prints the batch that fits, so trust it over this line.
case "$BACKBONE" in
  *vit_7b*)     DEF_BATCH=24 ;;
  *huge_plus*)  DEF_BATCH=32 ;;
  *vit_large*)  DEF_BATCH=48 ;;
  *)            DEF_BATCH=64 ;;
esac
BATCH="${BATCH:-$DEF_BATCH}"
EPOCHS="${EPOCHS:-60}"
# Both A100s are yours for the whole allocation; there is no reason to leave
# one idle. Effective batch = BATCH x GPUS x ACCUM.
GPUS="${GPUS:-2}"
ACCUM="${ACCUM:-1}"
ENVNAME="${ENVNAME:-dinov3}"

module load mldl/Miniconda
# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "$ENVNAME"

# Compute nodes have no route out. prefetch_weights.py must already have run
# on the login node; these point at that cache and make a missing file fail in
# seconds with a clear message rather than hanging on a connection that will
# never open and burning the allocation.
export HF_HOME="${HF_HOME:-/scratch/$USER/hf}"
export TORCH_HOME="${TORCH_HOME:-/scratch/$USER/torch}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

export OMP_NUM_THREADS=$(( SLURM_CPUS_PER_TASK / GPUS ))
export NCCL_DEBUG=WARN
# Single node: the two A100s talk over NVLink/PCIe, not the fabric. Disabling
# IB avoids a known NCCL stall on nodes where it is present but not configured
# for this partition. Remove both lines if you ever go multi-node.
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-0}

echo "node      : $(hostname)"
echo "gpus      : $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | paste -sd'; ')"
echo "backbone  : $BACKBONE"
echo "data      : $DATA"
echo "out       : $OUT"

# Fail here rather than 40 minutes in. Costs about two minutes.
python preflight.py --data "$DATA" --backbone "$BACKBONE" --batch "$BATCH" --steps 4

torchrun --standalone --nproc_per_node="$GPUS" train_roads.py \
    --data "$DATA" \
    --backbone "$BACKBONE" \
    --out "$OUT" \
    --epochs "$EPOCHS" \
    --batch "$BATCH" \
    --accum "$ACCUM" \
    --workers $(( SLURM_CPUS_PER_TASK / GPUS )) \
    --amp bf16 \
    --grad-ckpt

echo "done -> $OUT"
