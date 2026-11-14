#!/bin/bash
# One SLICE of ACPV-Net training, resumable, for chaining on a busy cluster.
#
# Why slices. PARAM's gpu partition schedules by backfill: a job runs when a
# gap appears that is big enough for its requested wall. A 30-hour 2-GPU
# request fits almost nothing, and ours was pushed back four times over three
# days - 13 Nov 23:14 -> 14 Nov 01:58 -> 14 Nov 20:48 -> 15 Nov 09:45. A
# 4-hour request fits gaps that open several times a day. Eight chained
# 4-hour slices therefore tend to FINISH SOONER in wall-clock than one
# 30-hour job, even though they do the same work, and nothing is lost when
# one is killed.
#
#   sbatch --parsable --time=04:00:00 slurm_acpv_slice.sh
#   sbatch --dependency=afterany:<prev> --time=04:00:00 slurm_acpv_slice.sh
#
# afterany, not afterok: a slice that ends at its own wall is recorded as
# TIMEOUT, not COMPLETED, so afterok would stall the chain for good. The
# price of afterany is that a BROKEN slice also lets the next one start, so
# every slice checks for itself that it is resuming when it ought to be -
# see REQUIRE_CKPT below.
#
# Resume is not automatic in this repo. scripts/train_iter.py:235 resumes
# only when BOTH cfg.RESUME and cfg.RESUME_CHECKPOINT are set, and it never
# reads the `last_checkpoint` pointer that the checkpointer writes
# (topomapper/utils/checkpoint.py:117). A chain that just re-ran the same
# command would train the first iterations over and over, with a loss curve
# that looked plausible the whole time. So this reads the pointer and passes
# the name explicitly.

#SBATCH --job-name=acpv-slice
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-task=32
#SBATCH --time=04:00:00
# Absolute, with %u for the user name: #SBATCH paths are relative to the
# directory sbatch was run FROM, and submitting from the wrong one is how
# job 116777 wrote its logs to ~/logs and then died because `python
# preflight.py` was not in the home directory either.
#SBATCH --output=/home/%u/dinov3-roads/logs/%x-%j.out
#SBATCH --error=/home/%u/dinov3-roads/logs/%x-%j.err

set -euo pipefail

# This script's own directory, not the cwd - same lesson as above.
CODE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$CODE/logs"

REPO="${REPO:-/scratch/$USER/ACPV-Net}"
CONFIG="${CONFIG:-config-files/ongole512_train.yaml}"
ENVNAME="${ENVNAME:-acpvnet}"
# Set REQUIRE_CKPT=1 on every slice after the first. The slice then refuses
# to run rather than quietly starting from zero, which is the one failure
# mode of this design that would waste days without looking wrong.
REQUIRE_CKPT="${REQUIRE_CKPT:-0}"

die() { echo "XXXXXXX $* XXXXXXX"; exit 1; }

echo "node   : $(hostname)"
echo "date   : $(date)"
echo "repo   : $REPO"
echo "config : $CONFIG"

# ---------- conda, found by its binary rather than the shell function ----------
CONDA_BASE=""
if [ -n "${CONDA_EXE:-}" ] && [ -x "${CONDA_EXE:-}" ]; then
    _b="$("$CONDA_EXE" info --base 2>/dev/null || true)"
    [ -n "$_b" ] && [ -f "$_b/etc/profile.d/conda.sh" ] && CONDA_BASE="$_b"
fi
if [ -z "$CONDA_BASE" ] && [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    CONDA_BASE="$HOME/miniconda3"
fi
[ -n "$CONDA_BASE" ] || die "no conda found; set CONDA_EXE"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$ENVNAME"

cd "$REPO"
export PYTHONPATH="$REPO:${PYTHONPATH:-}"
export HF_HOME="${HF_HOME:-/scratch/$USER/hf}"
export HF_HUB_OFFLINE=1

# ---------- the assets the repo does not ship ----------
for f in models/first_stage_models/kl-f4/model.ckpt \
         pretrained/vssm_small_0229_ckpt_epoch_222.pth ; do
  [ -f "$f" ] || die "MISSING: $REPO/$f"
done

# ---------- the Triton switch must be off ----------
# Job 116724 died three minutes in because VMamba's cross-scan asks Triton to
# JIT-compile a C launcher at the first forward pass, and the compute node has
# gcc but no stdlib.h. csm_triton.py:4 turns that path off; the pure-torch
# CrossScanF is used instead. Check it here, where it costs a second, rather
# than discovering it again after hours of queueing.
WT="$(python -c "from topomapper.backbones.csm_triton import WITH_TRITON; print(WITH_TRITON)" 2>/dev/null || echo UNKNOWN)"
echo "WITH_TRITON: $WT"
[ "$WT" = "False" ] || die "WITH_TRITON is '$WT'; set it False in topomapper/backbones/csm_triton.py:4"

# ---------- where this run's checkpoints live ----------
OUT="$(python -c "
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
print(d.get('OUTPUT_DIR') or '')
" "$CONFIG")"
[ -n "$OUT" ] || die "no OUTPUT_DIR in $CONFIG"
echo "output : $OUT"
mkdir -p "$OUT"

# ---------- resume, if there is anything to resume from ----------
# checkpoint.py writes the newest checkpoint's path into <OUT>/last_checkpoint.
# train_iter.py joins RESUME_CHECKPOINT onto OUTPUT_DIR itself (line 236), so
# it wants the bare NAME, not a path.
CKPT=""
if [ -f "$OUT/last_checkpoint" ]; then
    CKPT="$(basename "$(tr -d ' \t\r\n' < "$OUT/last_checkpoint")")"
    [ -f "$OUT/$CKPT" ] || { echo "  last_checkpoint names '$CKPT', which is not there - ignoring"; CKPT=""; }
fi

if [ -n "$CKPT" ]; then
    echo "resuming from: $CKPT"
    python scripts/train_iter.py --config-file "$CONFIG" \
        --resume --resume-checkpoint "$CKPT"
else
    # if/then, not `[ ] && die`: under `set -e` a failing test as the last
    # command of a list would exit the script with status 1 - so the ordinary
    # first-slice case would look like a failure and break the chain.
    if [ "$REQUIRE_CKPT" = "1" ]; then
        die "REQUIRE_CKPT=1 but no checkpoint in $OUT - refusing to restart from scratch. Check the previous slice's log."
    fi
    echo "no checkpoint - starting from scratch (this should be slice 1 only)"
    python scripts/train_iter.py --config-file "$CONFIG"
fi

# ---------- what the next slice will pick up ----------
echo
echo "=============== SLICE DONE ==============="
date
if [ -f "$OUT/last_checkpoint" ]; then
    echo "last_checkpoint: $(cat "$OUT/last_checkpoint")"
    ls -la "$OUT" | tail -5
else
    echo "WARNING: no last_checkpoint written - the next slice will refuse to run"
fi
