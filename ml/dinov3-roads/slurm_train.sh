#!/bin/bash -x
# Train the road segmenter on PARAM Rudra (IIT Patna).
#
# Configuration taken from the PARAM Rudra user manual, not guessed:
#   partition "gpu"   8 nodes; per node 2 x A100 80 GB HBM2e, 2 x Xeon
#                     G-6240R = 48 cores, 192 GB RAM, 800 GB local SSD  (p9)
#   walltime          default 2 h, max 4 days per job; up to 10 jobs at once;
#                     8 nodes for 4 days / 16 for 2 / 32 for 1          (p24)
#   gres              "User must specify #SBATCH --gres=gpu:1/2"        (p39)
#   conda             module purge; module load mldl/Miniconda          (p80)
#   quota             /home 50 GB soft, 60 hard; /scratch 200 GB soft,
#                     220 hard, 7-day grace                             (p39)
#   filesystem        Lustre, 1.0 PiB, 25 GB/s                          (p9)
#
# Validate before you ever queue this for real - it costs nothing and it is
# the one thing that protects your slot:
#
#   sbatch --test-only slurm_train.sh
#
# It parses every directive, rejects a bad one, and estimates when the job
# would start, without submitting anything (manual p31).
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
DATA="${DATA:-/scratch/$USER/ongole_roads_v2}"
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

# module purge first: this inherits the login shell's modules, where conda
# (base) is already active, and stacking another conda on top of that is how
# you end up running the wrong interpreter. The manual's own DL example
# purges (p80).
module purge
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

# ---------- stage the chips onto the node's local SSD ----------
# /scratch is Lustre, and this dataset is ~35,000 small files read in random
# order every epoch. Lustre is excellent at streaming big files and poor at
# metadata-heavy random access, which is exactly this access pattern; the GPU
# nodes each have an 800 GB local SSD that is very good at it. Copying once at
# job start costs a couple of minutes and buys it back every epoch.
#
# Entirely optional and fully guarded: any failure here leaves DATA pointing
# at Lustre and the job runs as before. Set STAGE_LOCAL=0 to skip it.
STAGE_LOCAL="${STAGE_LOCAL:-1}"
LOCAL_BASE="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}"
if [ "$STAGE_LOCAL" = "1" ] && [ -d "$LOCAL_BASE" ] && [ -w "$LOCAL_BASE" ]; then
    need_kb=$(( 12 * 1024 * 1024 ))          # 12 GB, against 9.4 GB of chips
    free_kb=$(df -Pk "$LOCAL_BASE" | awk 'NR==2{print $4}')
    if [ "${free_kb:-0}" -gt "$need_kb" ]; then
        STAGE="$LOCAL_BASE/ongole_chips.$SLURM_JOB_ID"
        echo "staging chips -> $STAGE"
        mkdir -p "$STAGE"
        # One big sequential read beats 35,000 small ones, so prefer the tar.
        if [ -f "/scratch/$USER/ongole_roads_lean.tar" ]; then
            tar -xf "/scratch/$USER/ongole_roads_lean.tar" -C "$STAGE" || true
        else
            cp -r "$DATA" "$STAGE/" || true
        fi
        staged="$STAGE/$(basename "$DATA")"
        if [ -f "$staged/dataset.json" ]; then
            DATA="$staged"
            echo "staged ok, training from local SSD"
            trap 'rm -rf "$STAGE"' EXIT
        else
            echo "staging incomplete - falling back to Lustre, which is fine"
            rm -rf "$STAGE"
        fi
    else
        echo "local SSD has only ${free_kb:-0} KB free - using Lustre"
    fi
fi

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
