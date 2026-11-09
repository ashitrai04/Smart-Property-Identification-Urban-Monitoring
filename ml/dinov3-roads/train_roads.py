"""Train a road-surface segmenter on a frozen DINOv3 backbone.

Why the backbone is frozen by default
-------------------------------------
DINOv3 ViT-7B/16 is 6.72 billion parameters. Fine-tuning it with AdamW needs
roughly bf16 weights (13 GB) + gradients (13 GB) + fp32 master weights (27 GB)
+ two optimizer moments (54 GB) = about 107 GB of state before a single
activation, so it only fits under FSDP across several 80 GB cards. The Ongole
road set is a few thousand chips cut from one city. Six billion parameters and
a few thousand correlated samples is not a training run, it is an overfitting
exercise, and the frozen-backbone-plus-light-head pattern is what the DINOv3
authors and everyone building on them actually do.

Frozen, the 7B needs ~13 GB for weights in bf16 and runs comfortably on one
A100-80. With --cache-features it runs once over the dataset and the decoder
then trains on cached tensors, which turns a 7B job into minutes per epoch.

Sizes that exist
----------------
    vit_small_patch16_dinov3        21 M     lvd1689m
    vit_small_plus_patch16_dinov3   29 M     lvd1689m
    vit_base_patch16_dinov3         86 M     lvd1689m
    vit_large_patch16_dinov3       300 M     lvd1689m, sat493m   <- in production now
    vit_huge_plus_patch16_dinov3   840 M     lvd1689m
    vit_7b_patch16_dinov3         6716 M     lvd1689m, sat493m
There is no 3B DINOv3. The step below 7B is ViT-H+ at 840 M, and that one has
no satellite-pretrained tag - only the web-image LVD-1689M weights.

The decoder
-----------
UPerNet (pyramid pooling + FPN) over four transformer taps, plus a
full-resolution detail branch. The detail branch is not decoration: at 20 cm a
16-pixel patch covers 3.2 m of ground, which is wider than a lane, so the
transformer physically cannot place a carriageway edge better than that. Every
sharp edge in the output comes from the detail branch and from the boundary
term in the loss.

Losses
------
BCE + Dice + Boundary, which is the combination the remote-sensing literature
settles on for thin linear features. Dice handles the class imbalance (road is
~3% of pixels here), BCE keeps the probabilities calibrated, and the boundary
term is what stops the result being round-cornered mush: plain Dice barely
changes when an edge is off by 5-10 px, so nothing in it pushes the edge to the
right place.

Usage
-----
  single GPU, quick:
    python train_roads.py --data /scratch/ongole_roads --backbone vit_large_patch16_dinov3.sat493m

  7B on one A100-80, features cached first:
    python train_roads.py --data /scratch/ongole_roads \
        --backbone vit_7b_patch16_dinov3.sat493m --cache-features --batch 8

  multi-GPU:
    torchrun --nproc_per_node=4 train_roads.py --data ... --backbone ...
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from torch.utils.data import DataLoader, Dataset, DistributedSampler

# decoders.py sits beside this file. Add that directory explicitly rather than
# relying on the working directory: SLURM may launch from anywhere, and
# preflight.py loads this module through importlib, where the script's own
# directory is not placed on the path for you.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# DINOv3 statistics. The satellite tag carries its own, and they are not
# ImageNet's; using the wrong pair costs real accuracy.
STATS = {
    "sat493m": ((0.430, 0.411, 0.296), (0.213, 0.156, 0.143)),
    "lvd1689m": ((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
}


# ─────────────────────────────── data ───────────────────────────────
class ChipSet(Dataset):
    def __init__(self, root, split, mean, std, train=True):
        self.img = sorted((Path(root) / "images" / split).glob("*.tif"))
        self.msk = Path(root) / "masks" / split
        self.mean = np.asarray(mean, np.float32)
        self.std = np.asarray(std, np.float32)
        self.train = train
        if not self.img:
            raise SystemExit(f"no chips in {Path(root) / 'images' / split}")

    def __len__(self):
        return len(self.img)

    def __getitem__(self, i):
        import rasterio
        p = self.img[i]
        with rasterio.open(p) as s:
            rgb = s.read([1, 2, 3]).transpose(1, 2, 0)
        m = np.asarray(Image.open(self.msk / f"{p.stem}.png").convert("L"))
        y = (m > 127).astype(np.float32)

        if self.train:
            # Dihedral only. Roads have no canonical orientation, so the eight
            # flips and rotations are free data; colour jitter is deliberately
            # absent because the backbone is frozen and its statistics are
            # fixed, so photometric noise only blurs what the decoder sees.
            k = np.random.randint(4)
            if k:
                rgb, y = np.rot90(rgb, k, (0, 1)), np.rot90(y, k, (0, 1))
            if np.random.rand() < 0.5:
                rgb, y = rgb[:, ::-1], y[:, ::-1]

        x = (np.ascontiguousarray(rgb).astype(np.float32) / 255.0 - self.mean) / self.std
        return (torch.from_numpy(x.transpose(2, 0, 1)),
                torch.from_numpy(np.ascontiguousarray(y))[None])


# ───────────────────────────── model ─────────────────────────────
def gn(c):
    """GroupNorm, not BatchNorm, throughout the decoder.

    Two reasons, both practical. A 7B backbone leaves room for a batch of 2-8
    per card, and BatchNorm's estimates get noisy exactly there, which is the
    regime this will actually run in. And the pyramid pooling branch reduces to
    1x1 spatially, where BatchNorm cannot compute a statistic at all and throws
    "Expected more than 1 value per channel" the moment a batch of one reaches
    it. GroupNorm does not care about batch size, so neither failure happens.
    """
    return nn.GroupNorm(min(32, c), c)


class UPerDecoder(nn.Module):
    def __init__(self, dim, ch=256, pools=(1, 2, 3, 6)):
        super().__init__()
        def cbr(i, o, k=3):
            return nn.Sequential(nn.Conv2d(i, o, k, padding=k // 2),
                                 gn(o), nn.ReLU(inplace=True))
        self.up = nn.Sequential(nn.ConvTranspose2d(dim, dim // 2, 2, 2),
                                gn(dim // 2), nn.ReLU(inplace=True))
        self.ppm = nn.ModuleList([
            nn.Sequential(nn.AdaptiveAvgPool2d(p), nn.Conv2d(dim, ch, 1),
                          gn(ch), nn.ReLU(inplace=True)) for p in pools])
        self.bottleneck = cbr(dim + len(pools) * ch, ch)
        self.lat = nn.ModuleList([cbr(dim // 2, ch, 1), cbr(dim, ch, 1), cbr(dim, ch, 1)])
        self.fpn = nn.ModuleList([cbr(ch, ch) for _ in range(3)])
        self.fuse = cbr(4 * ch, ch)

    def forward(self, feats):
        f = [self.up(feats[0]), feats[1], feats[2],
             F.max_pool2d(feats[3], 2)]
        deep = f[-1]
        ppm = [deep] + [F.interpolate(p(deep), size=deep.shape[-2:], mode="bilinear",
                                      align_corners=False) for p in self.ppm]
        lat = [l(z) for l, z in zip(self.lat, f[:-1])]
        lat.append(self.bottleneck(torch.cat(ppm, 1)))
        for i in range(len(lat) - 1, 0, -1):
            lat[i - 1] = lat[i - 1] + F.interpolate(lat[i], size=lat[i - 1].shape[-2:],
                                                    mode="bilinear", align_corners=False)
        outs = [self.fpn[i](lat[i]) for i in range(3)] + [lat[-1]]
        top = outs[0].shape[-2:]
        outs = [outs[0]] + [F.interpolate(o, size=top, mode="bilinear", align_corners=False)
                            for o in outs[1:]]
        return self.fuse(torch.cat(outs, 1))


class RoadNet(nn.Module):
    """Frozen DINOv3 + a decoder. See decoders.py for why 'sharp' is the default.

    Returns a single logit map in eval, and (logit, edge_logit) in training so
    the auxiliary boundary head can be supervised. The edge head exists because
    Dice barely moves when an edge is 5-10 px out, which is exactly the error
    that reads as a blobby road, so something in the loss has to care about it.
    """

    def __init__(self, backbone, taps=None, ch=256, freeze=True, grad_ckpt=False,
                 decoder="sharp"):
        super().__init__()
        import timm
        from decoders import DECODERS
        self.enc = timm.create_model(backbone, pretrained=True, num_classes=0)
        self.dim = self.enc.embed_dim
        n = len(self.enc.blocks)
        self.taps = list(taps) if taps else [n // 4 - 1, n // 2 - 1, 3 * n // 4 - 1, n - 1]
        self.frozen = freeze
        self.decoder_name = decoder
        if freeze:
            self.enc.eval()
            for p in self.enc.parameters():
                p.requires_grad_(False)
        elif grad_ckpt and hasattr(self.enc, "set_grad_checkpointing"):
            self.enc.set_grad_checkpointing(True)

        if decoder not in DECODERS:
            raise ValueError(f"decoder must be one of {list(DECODERS)}")
        self.head = DECODERS[decoder](self.dim, ch)

        if decoder == "uper":
            # The old path: the decoder stops at stride 8, so a shallow
            # full-resolution branch is bolted on and the two are mixed.
            self.detail = nn.Sequential(
                nn.Conv2d(3, 32, 3, padding=1), gn(32), nn.ReLU(inplace=True),
                nn.Conv2d(32, 64, 3, padding=1), gn(64), nn.ReLU(inplace=True))
            self.mix = nn.Sequential(
                nn.Conv2d(self.head.out_ch + 64, 128, 3, padding=1), gn(128),
                nn.ReLU(inplace=True),
                nn.Conv2d(128, 64, 3, padding=1), gn(64), nn.ReLU(inplace=True))
            final = 64
        else:
            # SharpDecoder already comes out at stride 1 with the stem fused
            # in at every step, so there is nothing to bolt on.
            self.detail = self.mix = None
            final = self.head.out_ch

        self.cls = nn.Conv2d(final, 1, 1)
        self.edge = nn.Conv2d(final, 1, 1)

    def features(self, x):
        ctx = torch.no_grad() if self.frozen else torch.enable_grad()
        with ctx:
            return self.enc.forward_intermediates(
                x, indices=self.taps, return_prefix_tokens=False, norm=True,
                output_fmt="NCHW", intermediates_only=True)

    def decode(self, feats, x):
        if self.mix is None:
            f = self.head(list(feats), x)
        else:
            f = self.head(list(feats))
            f = F.interpolate(f, size=x.shape[-2:], mode="bilinear", align_corners=False)
            f = self.mix(torch.cat([f, self.detail(x)], 1))
        return self.cls(f), self.edge(f)

    def forward(self, x):
        logit, edge = self.decode(self.features(x), x)
        return (logit, edge) if self.training else logit


# ───────────────────────────── losses ─────────────────────────────
def dice_loss(logit, y, eps=1.0):
    p = torch.sigmoid(logit)
    num = 2 * (p * y).sum((1, 2, 3)) + eps
    den = p.sum((1, 2, 3)) + y.sum((1, 2, 3)) + eps
    return (1 - num / den).mean()


_SOBEL = None


def boundary_loss(logit, y):
    """Penalise disagreement on edges, where Dice is almost blind.

    Dice changes very little when a boundary is 5-10 px out, which is exactly
    the error that reads as a blobby road. Comparing gradient magnitude of the
    prediction against the label's puts the pressure where it is needed. This
    is the cheap differentiable stand-in for the boundary losses in the
    remote-sensing literature - no distance transform per step.
    """
    global _SOBEL
    if _SOBEL is None or _SOBEL.device != logit.device:
        kx = torch.tensor([[-1., 0., 1.], [-2., 0., 2.], [-1., 0., 1.]])
        _SOBEL = torch.stack([kx, kx.T])[:, None].to(logit.device)
    p = torch.sigmoid(logit)
    gp = F.conv2d(p, _SOBEL.to(p.dtype), padding=1).abs().sum(1, keepdim=True)
    gy = F.conv2d(y, _SOBEL.to(y.dtype), padding=1).abs().sum(1, keepdim=True)
    return F.l1_loss(gp, gy)


def label_edges(y, width=2):
    """The label's own boundary band, as a target for the edge head.

    Dilate minus erode gives a band `width` pixels either side of the outline.
    A one-pixel-wide target would be almost impossible to hit and would make
    the edge loss mostly noise.
    """
    k = 2 * width + 1
    d = F.max_pool2d(y, k, 1, width)
    e = -F.max_pool2d(-y, k, 1, width)
    return (d - e).clamp(0, 1)


def total_loss(logit, y, edge_logit=None, w_bce=1.0, w_dice=1.0, w_bnd=0.5,
               w_edge=0.5, pos_weight=None):
    bce = F.binary_cross_entropy_with_logits(logit, y, pos_weight=pos_weight)
    loss = w_bce * bce + w_dice * dice_loss(logit, y) + w_bnd * boundary_loss(logit, y)
    if edge_logit is not None and w_edge > 0:
        et = label_edges(y)
        # The band is a few percent of pixels, so it needs its own positive
        # weight or the head learns to answer "not an edge" everywhere.
        pw = torch.tensor([10.0], device=logit.device, dtype=logit.dtype)
        loss = loss + w_edge * F.binary_cross_entropy_with_logits(
            edge_logit, et, pos_weight=pw)
    return loss, bce.detach()


def boundary_f1(pred, truth, tol=2):
    """Boundary F1: how well the two outlines agree within `tol` pixels.

    IoU is dominated by a road's interior and barely moves when the edge is
    soft, which is why a blobby result can still score well. This looks only
    at the outlines, so it is the number that actually says whether the shapes
    got sharper.
    """
    p = pred.float()
    t = truth.float()
    pe = label_edges(p, 1) > 0.5
    te = label_edges(t, 1) > 0.5
    k = 2 * tol + 1
    pe_d = F.max_pool2d(pe.float(), k, 1, tol) > 0.5
    te_d = F.max_pool2d(te.float(), k, 1, tol) > 0.5
    tp_p = (pe & te_d).sum().item()
    tp_t = (te & pe_d).sum().item()
    np_, nt = pe.sum().item(), te.sum().item()
    prec = tp_p / np_ if np_ else 0.0
    rec = tp_t / nt if nt else 0.0
    return (2 * prec * rec / (prec + rec)) if prec + rec else 0.0, np_, nt


# ───────────────────────────── metrics ─────────────────────────────
@torch.no_grad()
def evaluate(model, loader, device, amp_dtype, thr=0.5):
    model.eval()
    tp = fp = fn = 0
    bf_sum, bf_n = 0.0, 0
    for x, y in loader:
        x, y = x.to(device, non_blocking=True), y.to(device, non_blocking=True)
        with torch.autocast("cuda", dtype=amp_dtype):
            out = model(x)
            logit = out[0] if isinstance(out, tuple) else out
            p = torch.sigmoid(logit) >= thr
        t = y > 0.5
        tp += (p & t).sum().item(); fp += (p & ~t).sum().item(); fn += (~p & t).sum().item()
        bf, _, nt = boundary_f1(p, t)
        if nt:
            bf_sum += bf; bf_n += 1
    if dist.is_initialized():
        v = torch.tensor([tp, fp, fn], device=device, dtype=torch.float64)
        dist.all_reduce(v)
        tp, fp, fn = v.tolist()
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    iou = tp / (tp + fp + fn) if tp + fp + fn else 0.0
    model.train()
    return {"f1": f1, "precision": prec, "recall": rec, "iou": iou,
            "boundary_f1": bf_sum / bf_n if bf_n else 0.0}


# ───────────────────────────── train ─────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--backbone", default="vit_large_patch16_dinov3.sat493m")
    ap.add_argument("--out", default="runs/roads")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--backbone-lr", type=float, default=1e-5,
                    help="only used with --unfreeze-last")
    ap.add_argument("--wd", type=float, default=0.01)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--unfreeze-last", type=int, default=0,
                    help="fine-tune the last N transformer blocks (0 = fully frozen)")
    ap.add_argument("--grad-ckpt", action="store_true")
    ap.add_argument("--amp", default="bf16", choices=["bf16", "fp16", "off"])
    ap.add_argument("--w-bnd", type=float, default=0.5)
    ap.add_argument("--w-edge", type=float, default=0.5,
                    help="weight on the auxiliary boundary head (0 disables it)")
    ap.add_argument("--decoder", default="sharp", choices=["sharp", "uper"],
                    help="sharp = conv stem + progressive 16->1 fusion (default); "
                         "uper = the stride-8 UPerNet, kept for comparison")
    ap.add_argument("--accum", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    ddp = int(os.environ.get("WORLD_SIZE", 1)) > 1
    if ddp:
        dist.init_process_group("nccl")
        torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank if ddp else 0)
    rank0 = (not ddp) or dist.get_rank() == 0
    torch.manual_seed(a.seed)
    np.random.seed(a.seed)

    tag = "sat493m" if "sat493m" in a.backbone else "lvd1689m"
    mean, std = STATS[tag]
    if rank0:
        print(f"backbone {a.backbone}  normalisation {tag}  mean {mean}")

    tr = ChipSet(a.data, "train", mean, std, train=True)
    va = ChipSet(a.data, "val", mean, std, train=False)
    tsam = DistributedSampler(tr) if ddp else None
    vsam = DistributedSampler(va, shuffle=False) if ddp else None
    trl = DataLoader(tr, batch_size=a.batch, shuffle=tsam is None, sampler=tsam,
                     num_workers=a.workers, pin_memory=True, drop_last=True,
                     persistent_workers=a.workers > 0)
    val = DataLoader(va, batch_size=a.batch, shuffle=False, sampler=vsam,
                     num_workers=max(2, a.workers // 2), pin_memory=True)
    if rank0:
        print(f"train {len(tr):,} chips   val {len(va):,} chips")

    model = RoadNet(a.backbone, freeze=a.unfreeze_last == 0, grad_ckpt=a.grad_ckpt,
                    decoder=a.decoder).to(device)
    if a.unfreeze_last:
        blocks = model.enc.blocks
        for b in blocks[-a.unfreeze_last:]:
            for p in b.parameters():
                p.requires_grad_(True)
        model.frozen = False
    dec_params = [p for n_, p in model.named_parameters()
                  if p.requires_grad and not n_.startswith("enc.")]
    enc_params = [p for n_, p in model.named_parameters()
                  if p.requires_grad and n_.startswith("enc.")]
    if rank0:
        tot = sum(p.numel() for p in model.parameters())
        trn = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print(f"parameters: {tot/1e6:,.0f} M total, {trn/1e6:,.1f} M trainable "
              f"({100*trn/tot:.2f}%)")

    groups = [{"params": dec_params, "lr": a.lr}]
    if enc_params:
        groups.append({"params": enc_params, "lr": a.backbone_lr})
    opt = torch.optim.AdamW(groups, weight_decay=a.wd)
    steps = max(1, len(trl) // a.accum) * a.epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=[g["lr"] for g in groups], total_steps=steps, pct_start=0.1)

    if ddp:
        model = nn.parallel.DistributedDataParallel(
            model, device_ids=[local_rank], find_unused_parameters=False)

    amp_dtype = {"bf16": torch.bfloat16, "fp16": torch.float16}.get(a.amp, torch.float32)
    scaler = torch.cuda.amp.GradScaler(enabled=a.amp == "fp16")
    # Road is a small share of pixels; nudge BCE towards recall rather than
    # letting it win by predicting background everywhere.
    pw = torch.tensor([3.0], device=device)

    outdir = Path(a.out)
    if rank0:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "args.json").write_text(json.dumps(vars(a), indent=1), encoding="utf-8")

    best, hist = -1.0, []
    for ep in range(a.epochs):
        if tsam:
            tsam.set_epoch(ep)
        model.train()
        if a.unfreeze_last == 0:
            (model.module if ddp else model).enc.eval()   # keep frozen norms fixed
        t0, run = time.time(), 0.0
        opt.zero_grad(set_to_none=True)
        for i, (x, y) in enumerate(trl):
            x, y = x.to(device, non_blocking=True), y.to(device, non_blocking=True)
            with torch.autocast("cuda", dtype=amp_dtype, enabled=a.amp != "off"):
                out = model(x)
                logit, edge = out if isinstance(out, tuple) else (out, None)
                loss, _ = total_loss(logit.float(), y,
                                     edge_logit=None if edge is None else edge.float(),
                                     w_bnd=a.w_bnd, w_edge=a.w_edge, pos_weight=pw)
            scaler.scale(loss / a.accum).backward()
            if (i + 1) % a.accum == 0:
                scaler.unscale_(opt)
                torch.nn.utils.clip_grad_norm_(
                    [p for g in groups for p in g["params"]], 1.0)
                scaler.step(opt); scaler.update()
                opt.zero_grad(set_to_none=True)
                if sched.last_epoch < steps - 1:
                    sched.step()
            run += loss.item()
        m = evaluate(model, val, device, amp_dtype)
        if rank0:
            hist.append({"epoch": ep, "loss": run / max(1, len(trl)), **m})
            print(f"ep {ep:3d}  loss {run/max(1,len(trl)):.4f}  "
                  f"val F1 {m['f1']:.4f}  P {m['precision']:.3f}  R {m['recall']:.3f}  "
                  f"IoU {m['iou']:.4f}  bF1 {m['boundary_f1']:.4f}  "
                  f"{time.time()-t0:.0f}s", flush=True)
            (outdir / "history.json").write_text(json.dumps(hist, indent=1), encoding="utf-8")
            if m["f1"] > best:
                best = m["f1"]
                sd = (model.module if ddp else model).state_dict()
                # The frozen backbone is identical to the public weights, so
                # only the trained parts are saved: a few hundred MB instead of
                # 13 GB per checkpoint.
                if a.unfreeze_last == 0:
                    sd = {k: v for k, v in sd.items() if not k.startswith("enc.")}
                torch.save({"model": sd, "epoch": ep, "val_f1": best,
                            "boundary_f1": m["boundary_f1"], "decoder": a.decoder,
                            "backbone": a.backbone, "taps": (model.module if ddp else model).taps,
                            "frozen_backbone": a.unfreeze_last == 0},
                           outdir / "best.pth")
                print(f"        saved best.pth (F1 {best:.4f})", flush=True)
    if rank0:
        print(f"\nbest val F1 {best:.4f} -> {outdir/'best.pth'}")
    if ddp:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
