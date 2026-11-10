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
die()    { echo; echo "XXXXXXX FAILED AT STAGE $STAGE: $* XXXXXXX";
           # stderr goes to the .err file, which is a second place to look
           # when the whole point of this log is to be read on its own.
           echo "--- last 15 lines of stderr ---";
           tail -15 "${SLURM_SUBMIT_DIR:-.}/logs/acpv-prep-${SLURM_JOB_ID:-x}.err" 2>/dev/null;
           echo "Nothing after this ran."; exit 1; }

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
if [ -d "$DATA/train/heatmap" ] && [ "$(find "$DATA/train/heatmap" -type f 2>/dev/null | wc -l)" -gt 0 ]; then
    echo "already done: $(find "$DATA/train/heatmap" -type f | wc -l) files"
else
    [ -f data/generate_coco_vertex_heatmaps.py ] \
        || die "repo has no data/generate_coco_vertex_heatmaps.py - check its name with: ls data/*.py"
    python data/generate_coco_vertex_heatmaps.py \
        --annotation-path "$DATA/annotations/train.json" \
        --output-dir      "$DATA/train/heatmap" --sigma 3.0 \
        || die "generate_coco_vertex_heatmaps.py"
fi
n_hm=$(find "$DATA/train/heatmap" -type f 2>/dev/null | wc -l)
echo "  heatmaps: $n_hm"
[ "$n_hm" -gt 0 ] || die "no heatmaps produced"

# ---------- 3. D4 augmentation ----------
banner "D4 augmentation (expect about 8x the heatmaps)"
if [ -d "$DATA/train/heatmap_augmented" ] && [ "$(find "$DATA/train/heatmap_augmented" -type f 2>/dev/null | wc -l)" -ge "$n_hm" ]; then
    echo "already done: $(find "$DATA/train/heatmap_augmented" -type f | wc -l) files"
else
    [ -f data/augment_heatmap_d4.py ] || die "repo has no data/augment_heatmap_d4.py"
    python data/augment_heatmap_d4.py \
        --input_dir  "$DATA/train/heatmap" \
        --output_dir "$DATA/train/heatmap_augmented" \
        || die "augment_heatmap_d4.py"
fi
# Recursive. D4 augmentation writes one subdirectory per transform, so a
# top-level entry count reports "8" for what is really tens of thousands
# of files, and a stage that succeeded looks like it produced nothing.
n_aug=$(find "$DATA/train/heatmap_augmented" -type f 2>/dev/null | wc -l)
echo "  augmented: $n_aug"
[ "$n_aug" -gt 0 ] || die "no augmented heatmaps"

# ---------- 4. kl-4 latent encoding ----------
# The heavy one, and the one most likely to want a GPU: the script is called
# "accelerated" for a reason. On CPU it still works, just slowly, which is
# what the 12 hour walltime is for. If it refuses to run without CUDA that is
# a clean, early failure and the fix is a GPU job, not a code change.
banner "kl-4 latent encoding (the slow stage - hours on CPU)"
LAT="$DATA/train/heatmap_augmented_latent_kl-4"
if [ -d "$LAT" ] && [ "$(find "$LAT" -type f 2>/dev/null | wc -l)" -ge "$n_aug" ]; then
    echo "already done: $(find "$LAT" -type f | wc -l) latents"
else
    [ -f latent_encoder_accelerated.py ] || die "repo has no latent_encoder_accelerated.py"
    # Find the autoencoder config. This block was lost when stage 4 was
    # rewritten to loop over the D4 directories - the loop below still
    # referenced $KLCFG, and under `set -u` an unset variable is fatal:
    #   slurm_script: line 164: KLCFG: unbound variable
    # Ten seconds in, after stages 1-3 had been re-verified for nothing.
    #
    # Verified against the repo at HEAD: the config ships IN the repo as
    # config-files/autoencoder_kl_f4.yaml. The ommer-lab zip carries only
    # model.ckpt, which is why models/first_stage_models/kl-f4/config.yaml
    # never existed.
    KLCFG=""
    for c in config-files/autoencoder_kl_f4.yaml \
             models/first_stage_models/kl-f4/config.yaml \
             models/first_stage_models/kl-f4/*/config.yaml; do
        [ -f "$c" ] && { KLCFG="$c"; break; }
    done
    if [ -z "$KLCFG" ]; then
        KLCFG="$(find . -maxdepth 5 -iname "*.yaml" 2>/dev/null \
                 | grep -iE "kl.?f4|autoencoder" | head -1)"
    fi
    [ -n "$KLCFG" ] || die "no autoencoder config found - run: find $REPO -iname '*.yaml' | grep -i auto"
    echo "  autoencoder config: $KLCFG"
    # Verified against the repo, not assumed:
    #   augment_heatmap_d4.py writes output_dir/<transform>/<file>.npy - eight
    #     named subdirectories (line 84-86).
    #   latent_encoder_accelerated.py lists its input with os.listdir (line
    #     211), which is FLAT. Pointed at the parent it finds no .npy at all,
    #     prints "Found 0 files." and exits successfully having done nothing.
    #     That is what produced "latents: 1" from 59,112 inputs.
    #   latent_vertexheatmap_dataset.py reads
    #     heatmap_augmented_latent_kl-4/<transform>/<file>.npy, choosing a
    #     transform at random per sample (line 48-49).
    # So: encode each transform directory into a matching one.
    #
    # The scale factor is estimated from the data when not given. Estimating
    # it separately per directory would give eight slightly different scalings
    # of what must be one latent space, and the model would see the same road
    # at eight magnitudes. Estimate once, reuse everywhere.
    # Encode only the transforms the training config actually reads.
    #
    # latent_vertexheatmap_dataset.py:48 picks a transform per sample from
    # cfg.DATASETS.AUGMENTATIONS, and falls back to rot0 only when that list is
    # EMPTY. The Ongole config lists flip_h and flip_v, following Deventer's.
    # So encoding all eight D4 directories writes 59,112 latents of which
    # 44,334 are never opened - four times the CPU for nothing, on the stage
    # that dominates this job's runtime.
    #
    # Set TRANSFORMS="rot0 rot90 ..." to override, e.g. before widening
    # AUGMENTATIONS. The job is resumable, so adding more later re-encodes only
    # what is missing.
    if [ -z "${TRANSFORMS:-}" ]; then
        TRANSFORMS="$(python - <<PYEOF 2>/dev/null || echo ""
import yaml
try:
    d = yaml.safe_load(open("$CODE/acpv_ongole512_train.yaml", encoding="utf-8"))
    a = (d.get("DATASETS") or {}).get("AUGMENTATIONS") or []
except Exception:
    a = []
print(" ".join(str(x) for x in a))
PYEOF
)"
    fi
    [ -z "$TRANSFORMS" ] && TRANSFORMS="rot0"
    echo "  encoding transforms: $TRANSFORMS"
    for t in $TRANSFORMS; do
        [ -d "$DATA/train/heatmap_augmented/$t" ] \
            || die "the config asks for transform '$t' but $DATA/train/heatmap_augmented/$t does not exist - run: ls $DATA/train/heatmap_augmented"
    done

    SCALE=""
    for t in $TRANSFORMS; do
        d="$DATA/train/heatmap_augmented/$t/"
        [ -d "$d" ] || continue
        tname="$(basename "$d")"
        nin=$(find "$d" -maxdepth 1 -name "*.npy" | wc -l)
        [ "$nin" -gt 0 ] || { echo "  $tname: no .npy, skipped"; continue; }
        if [ -d "$LAT/$tname/z" ] && [ "$(find "$LAT/$tname/z" -maxdepth 1 -name "*.pt" | wc -l)" -ge "$nin" ]; then
            echo "  $tname: already encoded"
            continue
        fi
        echo "  $tname: encoding $nin files"
        if [ -z "$SCALE" ]; then
            python latent_encoder_accelerated.py \
                --config "$KLCFG" --input "$d" --output_dir "$LAT/$tname" \
                --type heatmap --batch-size 8 \
                --num-workers "${SLURM_CPUS_PER_TASK:-8}" 2>&1 | tee /tmp/enc.$$.log
            [ "${PIPESTATUS[0]}" = "0" ] || die "latent_encoder_accelerated.py on $tname"
            SCALE=$(grep -oE "scale_factor = [0-9.]+" /tmp/enc.$$.log | tail -1 | awk "{print $3}")
            rm -f /tmp/enc.$$.log
            echo "  scale factor fixed at $SCALE for the remaining transforms"
        else
            python latent_encoder_accelerated.py \
                --config "$KLCFG" --input "$d" --output_dir "$LAT/$tname" \
                --type heatmap --batch-size 8 \
                --num-workers "${SLURM_CPUS_PER_TASK:-8}" \
                --scale_factor "$SCALE" \
                || die "latent_encoder_accelerated.py on $tname"
        fi
        # .pt under z/, not .npy beside it: that is what the encoder writes
        # (latent_encoder_accelerated.py:243-258) and what the dataset loader
        # reads (latent_vertexheatmap_dataset.py:85). Counting the wrong thing
        # here failed a stage that had in fact succeeded.
        nout=$(find "$LAT/$tname/z" -maxdepth 1 -name "*.pt" 2>/dev/null | wc -l)
        echo "  $tname: $nout latents"
        [ "$nout" -ge "$nin" ] || die "$tname produced $nout latents from $nin inputs"
    done
fi
n_lat=$(find "$LAT" -type f 2>/dev/null | wc -l)
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
