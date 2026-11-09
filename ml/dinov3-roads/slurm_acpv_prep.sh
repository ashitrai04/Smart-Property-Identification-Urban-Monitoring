#!/bin/bash
# ACPV-Net preprocessing, unattended, on a compute node.
#
#   sbatch slurm_acpv_prep.sh
#   # tomorrow:
#   tail -40 logs/acpv-prep-<jobid>.out
#
# Everything between "the chips exist" and "the training job can be submitted":
# COCO conversion, vertex heatmaps, D4 augmentation, kl-4 latent encoding, and
# the dataset registration. It runs on the cpu partition, which is 122 nodes
# and effectively idle, so it starts immediately rather than queueing behind
# the five-day GPU backlog.
#
# NOT on the login node. 35,000-file extraction was killed there silently
# (manual p7: login-node CPU and memory limits terminate jobs), and this is a
# much heavier job than that was.
#
# Designed to be read tomorrow morning rather than watched. Every stage prints
# a banner, checks its own output, and STOPS at the first failure rather than
# cascading - so the last banner in the log is exactly where it got to, and
# the error underneath it is the real one rather than a downstream symptom.
#
# Safe to re-run: each stage is skipped if its output already exists.

#SBATCH --job-name=acpv-prep
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=32
#SBATCH --time=12:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -uo pipefail          # NOT -e: stages are checked explicitly, so a failure
                          # is reported with its stage name rather than killing
                          # the script silently at an unknown line.
mkdir -p logs

REPO="${REPO:-/scratch/$USER/ACPV-Net}"
CHIPS="${CHIPS:-/scratch/$USER/ongole_roads_v2}"
CODE="${CODE:-$HOME/dinov3-roads}"
DATA="$REPO/data/ongole_512"
STAGE=0

banner() { STAGE=$((STAGE+1)); echo; echo "=============== STAGE $STAGE: $* ==============="; date; }
die()    { echo; echo "XXXXXXX FAILED AT STAGE $STAGE: $* XXXXXXX"; echo "Nothing after this ran."; exit 1; }

echo "node  : $(hostname)"
echo "repo  : $REPO"
echo "chips : $CHIPS"

# ---------- conda ----------
CONDA_BASE=""
if [ -n "${CONDA_EXE:-}" ] && [ -x "${CONDA_EXE:-}" ]; then
    CONDA_BASE="$("$CONDA_EXE" info --base 2>/dev/null)"
fi
[ -z "$CONDA_BASE" ] && [ -n "${CONDA_PREFIX:-}" ] && CONDA_BASE="${CONDA_PREFIX%/envs/*}"
[ -f "$CONDA_BASE/etc/profile.d/conda.sh" ] || CONDA_BASE="$HOME/miniconda3"
[ -f "$CONDA_BASE/etc/profile.d/conda.sh" ] || die "no conda at $CONDA_BASE"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate acpvnet || die "conda activate acpvnet"
echo "python: $(which python)"

export PYTHONPATH="$REPO:${PYTHONPATH:-}"
# No GPU on this partition. Say so plainly so torch does not spend time
# probing, and so a script that assumes CUDA fails immediately and visibly
# rather than after an hour of work.
export CUDA_VISIBLE_DEVICES=""
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-8}"

cd "$REPO" || die "cd $REPO"

# ---------- 1. chips -> COCO ----------
banner "COCO conversion"
if [ -f "$DATA/annotations/train.json" ]; then
    echo "already done: $DATA/annotations/train.json"
else
    python "$CODE/acpv_prepare.py" --chips "$CHIPS" --repo "$REPO" \
        || die "acpv_prepare.py"
    [ -f "$DATA/annotations/train.json" ] || die "acpv_prepare.py wrote no train.json"
fi
python - <<PY || die "train.json is not readable COCO"
import json
d = json.load(open("$DATA/annotations/train.json"))
print(f"  {len(d['images']):,} images, {len(d['annotations']):,} polygons, "
      f"{len(d['categories'])} categories")
assert d["images"] and d["annotations"], "empty COCO file"
PY

# ---------- 2. vertex heatmaps ----------
banner "vertex heatmaps"
if [ -d "$DATA/train/heatmap" ] && [ "$(ls -1 "$DATA/train/heatmap" 2>/dev/null | wc -l)" -gt 0 ]; then
    echo "already done: $(ls -1 "$DATA/train/heatmap" | wc -l) files"
else
    [ -f data/generate_coco_vertex_heatmaps.py ] \
        || die "repo has no data/generate_coco_vertex_heatmaps.py - check its name with: ls data/*.py"
    python data/generate_coco_vertex_heatmaps.py \
        --annotation-path "$DATA/annotations/train.json" \
        --output-dir      "$DATA/train/heatmap" --sigma 3.0 \
        || die "generate_coco_vertex_heatmaps.py"
fi
n_hm=$(ls -1 "$DATA/train/heatmap" 2>/dev/null | wc -l)
echo "  heatmaps: $n_hm"
[ "$n_hm" -gt 0 ] || die "no heatmaps produced"

# ---------- 3. D4 augmentation ----------
banner "D4 augmentation (expect about 8x the heatmaps)"
if [ -d "$DATA/train/heatmap_augmented" ] && [ "$(ls -1 "$DATA/train/heatmap_augmented" 2>/dev/null | wc -l)" -gt "$n_hm" ]; then
    echo "already done: $(ls -1 "$DATA/train/heatmap_augmented" | wc -l) files"
else
    [ -f data/augment_heatmap_d4.py ] || die "repo has no data/augment_heatmap_d4.py"
    python data/augment_heatmap_d4.py \
        --input_dir  "$DATA/train/heatmap" \
        --output_dir "$DATA/train/heatmap_augmented" \
        || die "augment_heatmap_d4.py"
fi
n_aug=$(ls -1 "$DATA/train/heatmap_augmented" 2>/dev/null | wc -l)
echo "  augmented: $n_aug"
[ "$n_aug" -gt 0 ] || die "no augmented heatmaps"

# ---------- 4. kl-4 latent encoding ----------
# The heavy one, and the one most likely to want a GPU: the script is called
# "accelerated" for a reason. On CPU it still works, just slowly, which is
# what the 12 hour walltime is for. If it refuses to run without CUDA that is
# a clean, early failure and the fix is a GPU job, not a code change.
banner "kl-4 latent encoding (the slow stage - hours on CPU)"
LAT="$DATA/train/heatmap_augmented_latent_kl-4"
if [ -d "$LAT" ] && [ "$(ls -1 "$LAT" 2>/dev/null | wc -l)" -ge "$n_aug" ]; then
    echo "already done: $(ls -1 "$LAT" | wc -l) latents"
else
    [ -f latent_encoder_accelerated.py ] || die "repo has no latent_encoder_accelerated.py"
    [ -f models/first_stage_models/kl-f4/config.yaml ] \
        || die "kl-f4 config.yaml missing - the autoencoder zip may be only partly unpacked"
    python latent_encoder_accelerated.py \
        --config models/first_stage_models/kl-f4/config.yaml \
        --input      "$DATA/train/heatmap_augmented" \
        --output_dir "$LAT" \
        --type heatmap --batch-size 8 \
        || die "latent_encoder_accelerated.py (if it demands CUDA, rerun this stage as a GPU job)"
fi
n_lat=$(ls -1 "$LAT" 2>/dev/null | wc -l)
echo "  latents: $n_lat"
[ "$n_lat" -gt 0 ] || die "no latents produced"

# ---------- 5. register the dataset ----------
# Editing the repo's catalog in place, idempotently. Doing it from the job
# rather than by hand means tomorrow's state is reproducible from this log.
banner "register ongole_512 in paths_catalog.py"
python - <<'PY' || die "paths_catalog registration"
import io, re, sys, os
p = "topomapper/config/paths_catalog.py"
if not os.path.exists(p):
    sys.exit(f"{p} not found - the repo layout differs from what was expected")
s = io.open(p, encoding="utf-8").read()
if "ongole_512_train_with_latent_vertex_heatmap" in s:
    print("  already registered")
    sys.exit(0)
entry = """        'ongole_512_train_with_latent_vertex_heatmap': {
            'data_dir': 'data/ongole_512',
            'factory': 'LatentVertexHeatmapTrainDataset'
        },
        'ongole_512_test_with_latent_vertex_heatmap': {
            'data_dir': 'data/ongole_512',
            'factory': 'LatentVertexHeatmapTestDataset'
        },
"""
# Insert after the opening of the DATASETS dict, whatever it is called.
m = re.search(r"(DATASETS\s*=\s*\{\s*\n)", s)
if not m:
    sys.exit("could not find a DATASETS = { ... } dict to add to")
s = s[:m.end()] + entry + s[m.end():]
io.open(p, "w", encoding="utf-8").write(s)
print("  registered ongole_512 train and test")
PY

# ---------- 6. the training config ----------
banner "install the training config"
mkdir -p config-files
if [ -f "$CODE/acpv_ongole512_train.yaml" ]; then
    cp "$CODE/acpv_ongole512_train.yaml" config-files/ongole512_train.yaml \
        && echo "  config-files/ongole512_train.yaml written"
else
    die "no acpv_ongole512_train.yaml in $CODE"
fi

# ---------- done ----------
echo
echo "================= SUMMARY ================="
echo "COCO images   : $(python -c "import json;print(len(json.load(open('$DATA/annotations/train.json'))['images']))" 2>/dev/null)"
echo "heatmaps      : $n_hm"
echo "augmented     : $n_aug"
echo "latents       : $n_lat"
echo "config        : $REPO/config-files/ongole512_train.yaml"
echo
echo "PREPROCESSING COMPLETE. Next, when you are ready:"
echo "  sbatch --test-only --export=ALL,REPO=$REPO $CODE/slurm_acpv.sh"
echo "  sbatch            --export=ALL,REPO=$REPO $CODE/slurm_acpv.sh"
echo "==========================================="
date
