#!/bin/bash -x
# Unpack the chip archive on a COMPUTE node, not the login node.
#
#   sbatch slurm_extract.sh
#   squeue --me
#
# Why this exists. The first attempt ran `tar -xf` straight on login01 and
# stopped after 3,915 of 35,469 files, with no error. The manual explains it,
# page 7: "By default, there will be a limit on the CPU time that can be used
# on a Login Node by a user and there is a limit/user on the memory as well.
# If any of these are exceeded, the job will get terminated." Writing 35,000
# small files to Lustre is tens of thousands of metadata operations, and it
# crossed that limit. Nothing warns you; the extraction simply stops and the
# prompt returns, which looks exactly like success.
#
# The archive itself was fine - sha256sum -c passed - so this is purely about
# where the work runs. Page 17 is explicit that login nodes are for editing,
# transferring and submitting, and that jobs run on compute nodes.
#
# The cpu partition, not gpu: this is pure I/O and there is no reason to hold
# an A100 while it runs, or to queue behind GPU demand to get it.

#SBATCH --job-name=extract-chips
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail
mkdir -p logs

SCRATCH="/scratch/$USER"
TAR="${TAR:-$SCRATCH/ongole_roads_lean.tar}"
DEST="${DEST:-$SCRATCH}"
NAME="$(basename "$TAR" .tar)"
EXPECT="${EXPECT:-35469}"

echo "node    : $(hostname)"
echo "archive : $TAR"
echo "dest    : $DEST"

[ -f "$TAR" ] || { echo "no archive at $TAR"; exit 1; }

# Verify before unpacking. It costs a minute and it is the only thing that
# distinguishes a truncated transfer from a slow one.
if [ -f "$TAR.sha256" ]; then
    ( cd "$(dirname "$TAR")" && sha256sum -c "$(basename "$TAR").sha256" )
fi

entries=$(tar -tf "$TAR" | wc -l)
echo "archive lists $entries entries"

# Remove a half-extracted tree rather than unpacking on top of it: tar would
# happily leave the previous attempt's files in place and the count would
# still be wrong, for a different reason.
if [ -d "$DEST/ongole_roads_v2" ]; then
    have=$(find "$DEST/ongole_roads_v2" -type f | wc -l)
    echo "existing tree has $have files"
    if [ "$have" -lt "$EXPECT" ]; then
        echo "incomplete - removing it and starting clean"
        rm -rf "$DEST/ongole_roads_v2"
    else
        echo "already complete, nothing to do"
        exit 0
    fi
fi

time tar -xf "$TAR" -C "$DEST"

files=$(find "$DEST/ongole_roads_v2" -type f | wc -l)
echo "extracted $files files (expect $EXPECT)"
python - <<PY
import json, sys
d = json.load(open("$DEST/ongole_roads_v2/dataset.json"))
print("counts:", d["counts"])
PY

if [ "$files" -ne "$EXPECT" ]; then
    echo "FAILED: $files files, expected $EXPECT"
    exit 1
fi
echo "extraction complete and verified"
