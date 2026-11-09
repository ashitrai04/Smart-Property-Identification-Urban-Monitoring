# PARAM Rudra runbook — three models, start to finish

Every setting below comes from the PARAM Rudra (IIT Patna) user manual, not
from guesswork:

| | |
|---|---|
| login host | `paramrudra.iitp.ac.in` |
| SSH / SCP port | **4422** from outside IITP, 22 inside |
| GPU partition | `gpu` — 8 nodes, **2 × NVIDIA A100 80 GB HBM2e per node** |
| other partitions | `standard` (154, default), `cpu` (122), `hm` (32) |
| walltime | default **2 h**, maximum **4 days**; up to 10 jobs at once |
| scheduler | SLURM with **backfill** — an honest `-t` starts sooner |
| conda | `module load mldl/Miniconda` |
| filesystem | Lustre: `/home/vaibhavt_iitp` (quota'd) and `/scratch` |

Two A100-80 per node is comfortable. The 7B frozen is 13.4 GB of weights, so
it fits on **one** card with room for a real batch; the second card doubles
throughput through DDP.

---

## The three models

| | what it is | status |
|---|---|---|
| **DINOv3 ViT-7B** | `vit_7b_patch16_dinov3.sat493m`, 6716 M params, satellite-pretrained | train, frozen backbone |
| **DINOv3 ViT-L** | `vit_large_patch16_dinov3.sat493m`, 300 M, satellite-pretrained | train, the controlled baseline |
| **ACPV-Net** | VMamba-S + UPerNet + latent vertex heatmap, multi-class polygons | fine-tune from Deventer-512 |

**There is no DINOv3 3B.** The family is 21 M / 29 M / 86 M / 300 M / 840 M /
6716 M. The step below 7B is ViT-H+ at 840 M and it has **no satellite
weights**, only web images. A job naming a 3B dies at model creation, so the
third slot goes to ACPV-Net, which is a genuinely different architecture and
therefore a more informative comparison than another ViT size would be.

Measured baselines on this ground, so you know what to beat:

| | F1 vs `centre_part_Rd_v1` |
|---|---|
| current production DINOv3 ViT-L + UPerNet | **0.786** |
| raw mask, no geometry (the ceiling) | 0.790 |
| ACPV-Net zero-shot (Deventer weights) | 0.640 |
| LDPoly zero-shot (Deventer weights) | 0.404 |

---

## Chip size and resolution — decided

**512 px at 0.20 m**, 50% overlap.

- 0.20 m is where this imagery measures best: swept at 10/20/30/50/100 cm, F1
  peaked at 20 cm (0.653 on the first reference layer, 0.786–0.790 against
  `centre_part_Rd_v1`).
- 512 px at 0.20 m covers 102 m, which holds a whole junction — important,
  because a chip narrower than a junction teaches the model that roads end.
- 512 / 16 = a 32 × 32 token grid, the size DINOv3 is comfortable at, and the
  same 512 input ACPV-Net's Deventer-512 config expects. One chip set feeds
  all three models, so the comparison is clean.
- Yield, measured: **9,258 chips** (7,389 / 1,086 / 783).

---

## 1. Chip locally, then upload — the 45 GB mosaic never needs to go

The only thing on PARAM that would need the full orthomosaic is the final
1 km² inference, and that area can be cut here too. So nothing large travels:

| | size | actual |
|---|---|---|
| ~~`Ongole_ORI_V3_jpeg92.tif`~~ | ~~45 GB~~ | not uploaded |
| `ongole_roads_lean.tar` (chips, no duplicate PNGs) | **5.4 GiB** | upload this |
| ~~`ongole_roads.tar`~~ (chips incl. image PNGs) | ~~9.25 GiB~~ | superseded |
| `ongole_1km.tif` (inference area) | **235 MB** | upload |
| `dinov3-roads/` (code) | < 1 MB | upload |

The lean tar drops `images/*/*.png`. `train_roads.py:92` globs `images/*/*.tif`
only, so those PNGs - 3.82 GiB, 41% of the archive - were never read by
training. Mask PNGs and both vector files per chip are kept; they are needed.

Measured contents of the chip set as built:

| | train | val | test | total |
|---|---|---|---|---|
| chips | 7,389 | 1,086 | 783 | **9,258** |
| road pixels | 8.15% | 7.02% | 5.31% | ~7.9% |
| all-zero masks | 14.5% | 23.3% | 31.0% | 16.9% |

9.21 GiB on disk, 44,727 files. 512 px at 0.20 m = 102.4 m chips, 50% overlap,
EPSG:32644, split by 800 m spatial blocks (nearest train chip to any val chip:
154 m, median 311 m).

Of the candidate grid, 24,396 positions were dropped: 19,232 with under 1%
road (bar 10% of the fully road-free ones, kept as negatives - 1,564 chips, so
the model learns what is *not* road), 2,256 under 95% valid imagery, 2,908
straddling a split-block boundary.

**The test split is systematically sparser than train** - 5.31% road pixels
against 8.15%, a third of its masks empty against a seventh. Not a bug in the
split; the 800 m blocks the hash sent to test are genuinely more rural. So
test scores are not comparable to val scores. Report val as the headline and
treat test as its own, harder question.

### Build the chips (on your laptop)

```bash
cd "D:/React folder/Smart-Property-Identification-Urban-Monitoring/ml/dinov3-roads"

python chip_dataset.py \
    --image  "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif" \
    --labels "<extracted>/centre_part_Rd.shp" \
    --out    "C:/ongole_roads" \
    --gsd 0.20 --chip 512 --overlap 0.5

python make_infer_clip.py --out "C:/param_upload/ongole_1km.tif" --area-km2 1.0

bash package_for_param.sh C:/ongole_roads C:/param_upload
```

### Verify before you pack

```bash
python verify_dataset.py --data C:/ongole_roads --qa C:/param_upload/qa_sheet.png
```

Exits non-zero if the set is not fit to train on. It checks that every chip
has a mask and a vector and none is corrupt, that image and mask dimensions
agree and the CRS survived, the class balance, the dataset's own pixel
statistics against the normalisation the model will use, and — the one that
matters most — **spatial separation between train and val**. Chips overlap by
construction at 50% stride, so if the split were random the same street would
sit in both and the validation score would be fiction. It reports the distance
from each val chip to the nearest train chip and fails if any pair overlaps.

It also writes a contact sheet. Open it. The only reliable test of a label is
looking at it.

### Pack

`package_for_param.sh` tars the chip set and writes a SHA-256. **Tar matters
here**: the set is roughly 40,000 small files, `scp` pays a round trip per
file, and `/scratch` is Lustre, which is far happier writing a few big files
than forty thousand small ones. A directory copy that takes hours becomes one
stream.

### Upload

```bash
# code
scp -r -P 4422 "D:/React folder/Smart-Property-Identification-Urban-Monitoring/ml/dinov3-roads" \
    vaibhavt_iitp@paramrudra.iitp.ac.in:/home/vaibhavt_iitp/

ssh -p 4422 vaibhavt_iitp@paramrudra.iitp.ac.in "mkdir -p /scratch/\$USER/data"

# chips + checksum + inference clip + labels
scp -P 4422 C:/param_upload/ongole_roads_lean.tar        vaibhavt_iitp@paramrudra.iitp.ac.in:/scratch/vaibhavt_iitp/
scp -P 4422 C:/param_upload/ongole_roads_lean.tar.sha256 vaibhavt_iitp@paramrudra.iitp.ac.in:/scratch/vaibhavt_iitp/
scp -P 4422 C:/param_upload/ongole_1km.tif       vaibhavt_iitp@paramrudra.iitp.ac.in:/scratch/vaibhavt_iitp/data/
scp -P 4422 "<path>/centre_part_Rd_v1.zip" \
    vaibhavt_iitp@paramrudra.iitp.ac.in:/scratch/vaibhavt_iitp/data/
```

Use `rsync -avP -e "ssh -p 4422" <file> vaibhavt_iitp@paramrudra.iitp.ac.in:/scratch/vaibhavt_iitp/`
if the link drops — rsync resumes, `scp` starts over.

### Unpack and verify

```bash
ssh -p 4422 vaibhavt_iitp@paramrudra.iitp.ac.in
cd /scratch/$USER
sha256sum -c ongole_roads_lean.tar.sha256   # catches a truncated transfer now,
tar -xf ongole_roads_lean.tar               # not halfway through epoch 1
unzip data/centre_part_Rd_v1.zip -d data/
python -c "import json;d=json.load(open('ongole_roads_v2/dataset.json'));print(d['counts'])"
```

---

## 2. Environments and weights — login node, once

The login node has internet; **compute nodes do not**. Weights must be cached
here or every job hangs on a connection it will never get.

```bash
ssh -p 4422 vaibhavt_iitp@paramrudra.iitp.ac.in
cd ~/dinov3-roads
unzip /scratch/$USER/data/centre_part_Rd_v1.zip -d /scratch/$USER/data/

bash setup_param.sh all        # both conda envs + caches the 7B and ViT-L weights
```

That pulls 13.4 GB for the 7B. Check it landed:

```bash
du -sh /scratch/$USER/hf
```

---

## 3. Build the chips — ONLY IF you need to rebuild

Skip this. The chips are built and uploaded in step 1; the 45 GB mosaic is not
on PARAM, so this step cannot run as written unless you upload it. Kept for the
record, and in case you ever re-chip at a different GSD there.

```bash
sbatch --partition=cpu --time=04:00:00 --cpus-per-task=16 \
  --job-name=chips --output=logs/chips-%j.out --wrap "\
  module load mldl/Miniconda && \
  source \$(conda info --base)/etc/profile.d/conda.sh && conda activate dinov3 && \
  cd ~/dinov3-roads && python chip_dataset.py \
    --image /scratch/\$USER/data/Ongole_ORI_V3_jpeg92.tif \
    --labels /scratch/\$USER/data/centre_part_Rd.shp \
    --out /scratch/\$USER/ongole_roads \
    --gsd 0.20 --chip 512 --overlap 0.5"
```

Writes per chip: GeoTIFF + PNG, a binary mask, and the label as vector twice —
EPSG:4326 for GIS and chip-pixel coordinates for polygon-native models. The
train/val/test split is by **800 m spatial blocks**, not random: chips overlap
and sit on one street network, so a random split leaks val into train and
flatters every score.

```bash
python -c "import json;d=json.load(open('/scratch/$USER/ongole_roads/dataset.json'));print(d['counts'])"
```

---

## 4. Preflight — this is what protects your queue slot

Never `sbatch` a long job without it. Two minutes in a debug allocation,
on the real hardware:

```bash
salloc -p gpu --gres=gpu:1 -t 00:30:00
cd ~/dinov3-roads
module load mldl/Miniconda && source $(conda info --base)/etc/profile.d/conda.sh
conda activate dinov3
export HF_HOME=/scratch/$USER/hf

python preflight.py --data /scratch/$USER/ongole_roads \
    --backbone vit_7b_patch16_dinov3.sat493m --batch 8
```

It checks libraries, GPUs, that the weights are cached, that every chip has a
mask, builds the model, runs a real forward and backward, then prints **actual
peak memory**, ms/step, the batch size that fits, and the walltime to request.
It exits non-zero on failure, so it can gate the submission:

```bash
python preflight.py --data ... --backbone ... --batch 8 && sbatch slurm_train.sh
```

Verified behaviour: it catches an invented backbone tag (prints the real list)
and catches OOM ("peak 10.1 GB of 6.4 GB — halve --batch or add --grad-ckpt").

---

## 5. Train

```bash
cd ~/dinov3-roads

# ViT-L baseline first. It is 300 M and quick; if something is wrong with the
# data you find out in an hour, not after a 7B run.
sbatch --export=ALL,BACKBONE=vit_large_patch16_dinov3.sat493m,BATCH=16 slurm_train.sh

# then the 7B
sbatch --export=ALL,BACKBONE=vit_7b_patch16_dinov3.sat493m,BATCH=8 slurm_train.sh
```

Set `-t` from what preflight measured. The scheduler backfills, so asking 4
days for a 6-hour job just means waiting longer to start.

```bash
squeue -u $USER
tail -f logs/dinov3-roads-<jobid>.out
scancel <jobid>
```

**The backbone is frozen by default, deliberately.** Fine-tuning 6.7 B with
AdamW costs ~13 GB bf16 weights + 13 GB grads + 27 GB fp32 master + 54 GB
optimizer moments ≈ **107 GB of state before a single activation** — FSDP
across both cards and still tight. Against ~10 k correlated chips from one
city that is not training, it is memorising. Frozen, only the decoder learns
(tens of millions of parameters), which is the right capacity for this much
data and is what the DINOv3 authors do.

Checkpoints omit the frozen backbone, since it is byte-identical to the public
weights: a few hundred MB instead of 13 GB.

---

## 6. ACPV-Net

```bash
cd /scratch/$USER && git clone --depth 1 https://github.com/HeinzJiao/ACPV-Net.git
cd ACPV-Net

# three assets the repo needs but does not ship (login node, has internet)
mkdir -p models/first_stage_models/kl-f4 pretrained outputs
curl -L -o /tmp/kl-f4.zip https://ommer-lab.com/files/latent-diffusion/kl-f4.zip
unzip -o /tmp/kl-f4.zip -d models/first_stage_models/kl-f4
curl -L -o pretrained/vssm_small_0229_ckpt_epoch_222.pth \
  "https://github.com/MzeroMiko/VMamba/releases/download/%23v2cls/vssm_small_0229_ckpt_epoch_222.pth"
python -c "
from huggingface_hub import hf_hub_download
hf_hub_download('HeinzJiao/deventer512_vmamba-s_m_vh-ldm_kl4_b8',
                'deventer512_vmamba-s_m_vh-ldm_kl4_b8.zip', local_dir='outputs')"
unzip -o outputs/deventer512_vmamba-s_m_vh-ldm_kl4_b8.zip -d outputs

sbatch --export=ALL,REPO=/scratch/$USER/ACPV-Net slurm_acpv.sh
```

**If triton did not install, patch `topomapper/backbones/csm_triton.py` first.**
The repo advertises a pure-PyTorch fallback and the fallback is correct, but
the triton kernel below is decorated `@triton.jit` and annotates its arguments
`tl.tensor` / `tl.constexpr`. Decorators and annotations evaluate at *import*
time, so without triton the module dies on `NameError: name 'triton' is not
defined` and takes the whole backbone with it. In the `except` branch at the
top of that file, add stubs:

```python
    class _TritonStub:
        def __getattr__(self, name): return _TritonStub()
        def __call__(self, *a, **k):
            if len(a) == 1 and callable(a[0]) and not k: return a[0]
            return _TritonStub()
    triton = _TritonStub(); tl = _TritonStub()
```

Nothing that touches them ever executes — `cross_scan_fn` already picks the
torch path when `WITH_TRITON` is false. Confirmed working on the Ongole chips.

Its dataset registration and a training config for Ongole still need writing
against the chips from step 3; the inference path is already proven (see
below).

---

## 7. Inference on 1 km², on PARAM

```bash
sbatch --export=ALL,CKPT=/scratch/$USER/runs/<run>/best.pth slurm_infer.sh
```

One GPU, one hour — 1 km² at 20 cm is 5000 × 5000 px, about 150 tiles. Writes:

```
probability.tif   float32, georeferenced
mask.tif          uint8, georeferenced
mask.png          overlay.png
roads.gpkg        smoothed polygons + centrelines, EPSG:4326
report.json       settings, timing, and F1/precision/recall/IoU vs your labels
```

Tested end to end locally on real Ongole imagery with an untrained checkpoint:
16 tiles, 3 s, both GeoPackage layers written, scoring works. It **refuses** a
checkpoint whose decoder does not match, rather than loading half a network
and predicting confident nonsense.

---

## 8. Bring the results home

```bash
scp -r -P 4422 vaibhavt_iitp@paramrudra.iitp.ac.in:/scratch/vaibhavt_iitp/runs/<run>/best.pth .
scp -r -P 4422 vaibhavt_iitp@paramrudra.iitp.ac.in:/scratch/vaibhavt_iitp/infer/<run>-1km .
```

A frozen-backbone checkpoint is a few hundred MB. It drops straight into
`gpu-server/app/roadnet_arch.py` on the live server — the decoder here is
deliberately the same shape.

---

## What the labels actually are — measured, not inferred

I owe you a correction here. An earlier version of this file said
`centre_part_Rd_v1` was "OSM centrelines buffered to a fixed width per road
class", citing its `BUFF_DIST`, `width_m`, `width_fcla` and `fclass` columns.
I had read the column *names* and not their contents. Read the contents:

```
features: 1          <- the whole network is ONE dissolved polygon
BUFF_DIST  0.0       width_m     0.0      area_m2  0.0
osm_id     <null>    fclass      <null>   width_src <null>
```

Every one of those fields is empty or zero. It is a leftover schema from some
earlier tool, carrying no information. The claim was unfounded.

What the layer really is: **258.2 ha of hand-digitised road area**, dissolved
into a single connected polygon, with no attributes.

And the widths are not constant. Distance transform on the centreline of 250
chips, 238,437 samples:

| | width |
|---|---|
| p5 | 1.44 m |
| p25 | 2.91 m |
| **median** | **3.60 m** |
| p75 | 4.95 m |
| p95 | 10.52 m |

mean 4.34 m, **coefficient of variation 0.62**. A constant-width buffer would
sit near zero. These labels track the real carriageway — narrow where the lane
narrows, wide at the junctions and the main roads.

**This matters for what you should expect.** The earlier version of this note
told you not to hope for much, because constant-width ribbons cannot teach
shape. That was advice built on a false premise. Shape *is* in this ground
truth, so the decoder work has something real to learn from and the experiment
has genuine headroom.

The honest constraint is a different one, and it is about resolution rather
than labels. A 3.60 m median road at 0.20 m GSD is 18 px. At patch 16 that is
**1.1 tokens** — the typical road is thinner than one token of the backbone's
output grid. No amount of backbone capacity fixes that, and an 8x bilinear
upsample of a 32x32 grid is a blur kernel. It is why `SharpDecoder` carries a
convolutional stem at strides 1/2/4/8 straight off the image and fuses it on
the way up, and why training adds an explicit edge loss. Judge the 7B on
boundary F1, which is reported, not on IoU, which road interiors dominate and
which barely moves when the edges go soft.

One question still open, and only you can answer it: does the digitising
deliberately exclude plot-access tracks and informal lanes? It changes how to
read recall on the 1 km2 test - a "miss" on something never labelled is not a
miss.
