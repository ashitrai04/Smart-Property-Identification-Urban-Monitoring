#!/bin/bash
# Submit N chained ACPV-Net slices, so one command runs the whole training.
#
#   bash chain_acpv.sh [slices] [wall]
#   bash chain_acpv.sh 8 04:00:00      # the default
#
# Each slice resumes from the previous one's last checkpoint. Slice 1 may
# start from scratch; every slice after it is submitted with REQUIRE_CKPT=1,
# so if the chain is ever broken it STOPS instead of silently retraining the
# first iterations again - which is the one way this design could waste days
# while every log looked healthy.
#
# The dependency is afterany because a slice stopped by its own wall is
# recorded as TIMEOUT, not COMPLETED, and afterok would stall the chain
# permanently on the first such slice. REQUIRE_CKPT is what makes afterany
# safe.
#
# Training stops by itself when SOLVER.TOTAL_ITERS is reached, so leftover
# slices in the chain simply resume, find nothing to do and exit. Submitting
# a couple more than needed costs nothing.
set -euo pipefail

CODE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLICES="${1:-8}"
WALL="${2:-04:00:00}"

echo "chaining $SLICES slices of $WALL each"
echo

# --export=ALL carries the environment, which is how CONDA_EXE reaches the
# compute node - conda is a shell function and does not survive on its own.
J="$(sbatch --parsable --time="$WALL" --export=ALL "$CODE/slurm_acpv_slice.sh")"
echo "slice  1: $J   (may start from scratch)"

for i in $(seq 2 "$SLICES"); do
    J="$(sbatch --parsable --time="$WALL" \
                --dependency=afterany:"$J" \
                --export=ALL,REQUIRE_CKPT=1 \
                "$CODE/slurm_acpv_slice.sh")"
    printf 'slice %2d: %s   (afterany previous, must find a checkpoint)\n' "$i" "$J"
done

echo
squeue -u "$USER" --start -o "%.9i %.12j %.2t %.11l %.20S %R"
echo
echo "Watch with:  bash ~/status.sh"
echo "Stop the chain with:  scancel -u \$USER --name=acpv-slice"
