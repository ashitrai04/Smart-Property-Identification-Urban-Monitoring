# Road extraction on DINOv3 — chipping and training

Training set builder and trainer for road-surface segmentation over the Ongole
drone orthomosaic, on a DINOv3 backbone. Written to run on PARAM Rudra under
SLURM.

```
chip_dataset.py      ortho + road polygons -> chips (image, mask, vector), spatially split
prefetch_weights.py  pull DINOv3 weights on the login node, before any job queues
train_roads.py       frozen DINOv3 + UPerNet decoder + detail branch
slurm_train.sh       sbatch wrapper
```

---

## Read this before spending cluster hours

**The labels are the ceiling, and they are currently the problem.**

`centre_part_Rd_v1` is a single dissolved polygon of 258.2 ha carrying
`BUFF_DIST`, `width_m`, `width_fcla` and `fclass` columns. Those are OSM
centrelines buffered to a fixed width per road class — not traced carriageway
edges. Cut chips and look at them: the label is a constant-width ribbon lying
over a track whose real width varies along its length, and it stays straight
where the track bends.

A model learns the function it is shown. Trained on constant-width ribbons it
will output constant-width ribbons, and no decoder, no boundary loss and no
amount of backbone will change that. If the goal is "proper shapes, not
blobby", the highest-value work is not a bigger model — it is a few hundred
chips with the carriageway edge actually digitised. On the evidence so far,
300 hand-traced chips would beat 2,557 OSM-buffered ones for shape quality,
and would cost less than one 7B training run.

The current production model already scores **F1 0.786** against these same
labels, against a ceiling of 0.790 for the raw mask. There is almost nothing
left to win against this ground truth. What a new model can win is *shape*,
and shape is exactly what these labels cannot teach.

**There is no DINOv3 3B.** The family, confirmed against the installed timm:

| model | params | tags |
|---|---|---|
| `vit_small_patch16_dinov3` | 21 M | lvd1689m |
| `vit_small_plus_patch16_dinov3` | 29 M | lvd1689m |
| `vit_base_patch16_dinov3` | 86 M | lvd1689m |
| `vit_large_patch16_dinov3` | 300 M | lvd1689m, **sat493m** ← in production now |
| `vit_huge_plus_patch16_dinov3` | 840 M | lvd1689m only |
| `vit_7b_patch16_dinov3` | 6716 M | lvd1689m, **sat493m** |

The step below 7B is ViT-H+ at 840 M, and it has no satellite-pretrained
weights. So "7B and 3B" is really a choice between 300 M and 6.7 B if you want
to stay on satellite pretraining.

---

## 1. Build the dataset

```bash
python chip_dataset.py \
    --image  /scratch/Ongole_ORI_V3_jpeg92.tif \
    --labels /scratch/centre_part_Rd.shp \
    --out    /scratch/ongole_roads \
    --gsd 0.20 --chip 512 --overlap 0.5
```

Per chip it writes a georeferenced GeoTIFF and a PNG, a binary mask, and the
label as vector twice — `*.geojson` in EPSG:4326 for GIS, and `*_px.geojson`
in chip pixel coordinates so a polygon-native model can train on it without
carrying a transform around.

**The split is spatial, not random.** Chips overlap and sit on one continuous
street network, so a random split puts the same street in train and val and
every score comes out flattering. Whole 800 m blocks go to train / val / test,
assigned by a hash of the block index so rebuilding the dataset at another
chip size moves nothing between splits.

Expected yield over the labelled area, all of which is covered by flown
imagery:

| chip | GSD | ground | chips on road | at 50% overlap |
|---|---|---|---|---|
| 512 | 10 cm | 51 m | 7,708 | 30,830 |
| 512 | **20 cm** | 102 m | **2,557** | **10,227** |
| 512 | 30 cm | 154 m | 1,317 | 5,267 |
| 1024 | 20 cm | 205 m | 819 | 3,274 |

20 cm is the recommended starting point: it is where the production model
measures best, and 512 px there covers 102 m, which holds a whole junction.

---

## 2. Fetch weights on the login node

```bash
export HF_HOME=$SCRATCH/hf
python prefetch_weights.py vit_large_patch16_dinov3.sat493m
python prefetch_weights.py vit_7b_patch16_dinov3.sat493m    # 13.4 GB
```

Compute nodes usually have no internet. Without this, `pretrained=True` stalls
on a connection it will never get and the allocation burns until the walltime
kills it. `slurm_train.sh` sets `HF_HUB_OFFLINE=1` so a missing file fails
immediately and says so.

---

## 3. Train

```bash
# 300 M, the sane baseline
sbatch slurm_train.sh

# 6.7 B
sbatch --export=BACKBONE=vit_7b_patch16_dinov3.sat493m,BATCH=2,GPUS=4 slurm_train.sh
```

### The backbone is frozen by default, deliberately

Fine-tuning 6.7 B parameters with AdamW costs roughly 13 GB of bf16 weights +
13 GB of gradients + 27 GB of fp32 master weights + 54 GB of optimizer moments
≈ **107 GB of state before a single activation** — FSDP across several 80 GB
cards. And the dataset is a few thousand correlated chips from one city.
Six billion parameters against that is not training, it is memorising.

Frozen, the 7B needs ~13.4 GB in bf16 and runs on one A100-80 with room to
spare. Only the decoder trains — a few tens of millions of parameters, which
is the right capacity for this much data. This is also what the DINOv3 authors
do, and what HOTOSM's building model does with a frozen ViT-S plus a UperNet
decoder.

`--unfreeze-last N` fine-tunes the last N transformer blocks at a lower LR if
you want to try it. Do that only after the frozen baseline is working, and
expect it to need gradient checkpointing.

### Why the output can be sharp at all

At 20 cm a 16-pixel patch covers **3.2 m of ground** — wider than a traffic
lane. The transformer physically cannot localise a carriageway edge finer than
that. Sharpness comes from two other places:

- a **full-resolution detail branch** (3→32→64 convolutions on the raw image)
  concatenated with the upsampled UPerNet features before the classifier;
- a **boundary term** in the loss. Dice barely moves when an edge is 5–10 px
  out, which is precisely the error that reads as blobby, so the loss is
  `BCE + Dice + 0.5 × boundary`, the boundary part comparing Sobel gradient
  magnitude of prediction against label.

If you need genuinely crisp corners rather than smooth edges, the literature
points at frame-field learning (Girard et al.), which predicts a direction
field alongside the mask and polygonises along it for two extra convolutions.
That is the natural next step, and it needs labels with real corners in them —
back to the first section.

### Other decisions worth knowing

- **GroupNorm, not BatchNorm**, throughout the decoder. A 7B backbone leaves
  room for a batch of 2–8, where BatchNorm's statistics are noisy, and the
  pyramid-pooling branch reduces to 1×1 where BatchNorm cannot compute a
  statistic at all.
- **Normalisation follows the tag.** `sat493m` carries mean (0.430, 0.411,
  0.296) / std (0.213, 0.156, 0.143), which is *not* ImageNet. The script picks
  the right pair from the backbone name.
- **Augmentation is dihedral only** — eight flips and rotations. No colour
  jitter: the backbone is frozen, so its statistics are fixed and photometric
  noise only blurs what the decoder sees.
- **Checkpoints exclude the frozen backbone**, since it is byte-identical to
  the public weights. A few hundred MB per checkpoint instead of 13 GB.
- **`pos_weight=3`** on the BCE term. Road is roughly 3% of pixels here, and
  without it the cheapest way to a low loss is to predict background.

### Worth testing, not assuming

One 2026 few-shot remote-sensing study found the general-domain DINOv3 ViT-L
*beat* the satellite-pretrained variant on overhead imagery. That is a single
benchmark and not this task, but it is cheap to check: run the baseline twice,
once with `.sat493m` and once with `.lvd1689m`, before committing GPU-weeks to
the 7B.

---

## 4. Use the result

The decoder here is deliberately the same shape as the one in
`gpu-server/app/roadnet_arch.py`, so a trained checkpoint drops into the live
server. Post-process with `gpu-server/app/roadreg.py`, whose `smooth` mode
costs 0.005 F1 against the raw mask and removes the stair-stepping.
