"""Train the SharpDecoder on a DINOv3 ViT-7B backbone split across two GPUs.

SELF-CONTAINED ON PURPOSE. The only local import is shard.py; the dataset,
decoder and losses are inlined rather than imported from train_roads.py,
because the machine this runs on has the chips but not that tree. The
inlined code is a faithful copy - if you change a loss there, change it here.

Deliberately a separate file from train_roads.py: that script trains the
ViT-L run in production at F1 0.837, and the way not to break a working
thing is not to edit it.

What differs from the ViT-L recipe, and only this:

  * The backbone is 6.716 B parameters. fp32 is 26.9 GB, which does not fit a
    40 GB card other jobs are already using. It is held in bf16 (12.5 GiB),
    lossless in practice because it is frozen and every forward already runs
    under bf16 autocast.

  * 12.5 GiB still does not fit ONE card when ~14 GB is free, so the 40
    blocks are split across both. Pipeline parallelism, not DDP: DDP puts a
    full copy on each card and would fix nothing. Measured on this box with
    ViT-L, sharding cost 0.34 GiB peak per card and 0.19 s per forward.

  * Only the decoder trains, so nothing backpropagates across the device
    boundary and there is no pipeline schedule to get wrong.

The checkpoint carries backbone / decoder / taps / frozen_backbone exactly as
train_roads.py writes them, so gpu-server's roadnet_sharp.build() rebuilds it
without knowing it came from here.

  export HF_HOME=/workspace/proj/users/ashittbi/hf
  export HF_TOKEN=hf_xxx          # the 7B repo is gated
  python train_roads_7b.py --data /workspace/proj/users/ashittbi/chips/ongole_roads_v2 \
      --devices cuda:0,cuda:1 --batch 4 --accum 12 --epochs 60 \
      --out /workspace/proj/users/ashittbi/runs/vit7b
"""
import argparse, json, os, sys, time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from torch.utils.data import DataLoader, Dataset

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import shard as shardmod

# DINOv3 normalisation. sat493m is NOT ImageNet - using the wrong one
# measurably lowers the score.
STATS = {
    "sat493m": ((0.430, 0.411, 0.296), (0.213, 0.156, 0.143)),
    "lvd1689m": ((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
}


# ─────────────────────────────── data ───────────────────────────────
class ChipSet(Dataset):
    def __init__(self, root, split, mean, std, train=True):
        self.img = sorted((Path(root) / "images" / split).glob("*.tif"))
        if not self.img:
            self.img = sorted((Path(root) / "images" / split).glob("*.png"))
        self.msk = Path(root) / "masks" / split
        self.mean = np.asarray(mean, np.float32)
        self.std = np.asarray(std, np.float32)
        self.train = train
        if not self.img:
            raise SystemExit("no chips in %s" % (Path(root) / "images" / split))
        if not self.msk.is_dir():
            raise SystemExit("no masks dir at %s" % self.msk)

    def __len__(self):
        return len(self.img)

    def __getitem__(self, i):
        p = self.img[i]
        if p.suffix.lower() in (".tif", ".tiff"):
            import rasterio
            with rasterio.open(p) as s:
                rgb = s.read([1, 2, 3]).transpose(1, 2, 0)
        else:
            rgb = np.asarray(Image.open(p).convert("RGB"))
        m = np.asarray(Image.open(self.msk / ("%s.png" % p.stem)).convert("L"))
        y = (m > 127).astype(np.float32)

        if self.train:
            # Dihedral only. Roads have no canonical orientation, so the eight
            # flips and rotations are free data; colour jitter is deliberately
            # absent because the backbone is frozen and its statistics fixed.
            k = np.random.randint(4)
            if k:
                rgb, y = np.rot90(rgb, k, (0, 1)), np.rot90(y, k, (0, 1))
            if np.random.rand() < 0.5:
                rgb, y = rgb[:, ::-1], y[:, ::-1]

        x = (np.ascontiguousarray(rgb).astype(np.float32) / 255.0 - self.mean) / self.std
        return (torch.from_numpy(x.transpose(2, 0, 1)),
                torch.from_numpy(np.ascontiguousarray(y))[None])


# ───────────────────────────── decoder ─────────────────────────────
def gn(c):
    """GroupNorm everywhere: a 7B backbone forces small batches, where
    BatchNorm's statistics are noisy, and the pooling branch reduces to 1x1
    where BatchNorm cannot compute a statistic at all."""
    return nn.GroupNorm(min(32, c), c)


def cbr(i, o, k=3):
    return nn.Sequential(nn.Conv2d(i, o, k, padding=k // 2), gn(o), nn.ReLU(inplace=True))


class ConvStem(nn.Module):
    """Real multi-scale detail from the image, which the token grid lacks."""

    def __init__(self, widths=(32, 64, 96, 128)):
        super().__init__()
        w1, w2, w4, w8 = widths
        self.s1 = nn.Sequential(cbr(3, w1), cbr(w1, w1))
        self.s2 = nn.Sequential(nn.Conv2d(w1, w2, 3, 2, 1), gn(w2), nn.ReLU(inplace=True), cbr(w2, w2))
        self.s4 = nn.Sequential(nn.Conv2d(w2, w4, 3, 2, 1), gn(w4), nn.ReLU(inplace=True), cbr(w4, w4))
        self.s8 = nn.Sequential(nn.Conv2d(w4, w8, 3, 2, 1), gn(w8), nn.ReLU(inplace=True), cbr(w8, w8))
        self.widths = widths

    def forward(self, x):
        a = self.s1(x); b = self.s2(a); c = self.s4(b); d = self.s8(c)
        return a, b, c, d


class SharpDecoder(nn.Module):
    def __init__(self, dim, ch=256, pools=(1, 2, 3, 6), stem=(32, 64, 96, 128)):
        super().__init__()
        self.ppm = nn.ModuleList([
            nn.Sequential(nn.AdaptiveAvgPool2d(p), nn.Conv2d(dim, ch, 1),
                          gn(ch), nn.ReLU(inplace=True)) for p in pools])
        self.bottleneck = cbr(dim + len(pools) * ch, ch)
        # The four taps are all stride 16; 1x1 projections, then summed. No
        # pretend pyramid - the pyramid comes from the stem.
        self.proj = nn.ModuleList([cbr(dim, ch, 1) for _ in range(3)])
        self.merge = cbr(ch, ch)
        self.stem = ConvStem(stem)
        w1, w2, w4, w8 = stem
        self.up8 = cbr(ch + w8, 192)
        self.up4 = cbr(192 + w4, 128)
        self.up2 = cbr(128 + w2, 96)
        self.up1 = cbr(96 + w1, 64)
        self.out_ch = 64

    def forward(self, feats, image):
        deep = feats[-1]
        ppm = [deep] + [F.interpolate(p(deep), size=deep.shape[-2:], mode="bilinear",
                                      align_corners=False) for p in self.ppm]
        x = self.bottleneck(torch.cat(ppm, 1))
        for p, f in zip(self.proj, feats[:-1]):
            x = x + p(f)
        x = self.merge(x)

        s1, s2, s4, s8 = self.stem(image)

        def up(z, skip, block):
            z = F.interpolate(z, size=skip.shape[-2:], mode="bilinear", align_corners=False)
            return block(torch.cat([z, skip], 1))

        x = up(x, s8, self.up8)
        x = up(x, s4, self.up4)
        x = up(x, s2, self.up2)
        x = up(x, s1, self.up1)
        return x


# ───────────────────────────── losses ─────────────────────────────
def dice_loss(logit, y, eps=1.0):
    p = torch.sigmoid(logit)
    num = 2 * (p * y).sum((1, 2, 3)) + eps
    den = p.sum((1, 2, 3)) + y.sum((1, 2, 3)) + eps
    return (1 - num / den).mean()


_SOBEL = None


def boundary_loss(logit, y):
    """Penalise disagreement on edges, where Dice is almost blind."""
    global _SOBEL
    if _SOBEL is None or _SOBEL.device != logit.device:
        kx = torch.tensor([[-1., 0., 1.], [-2., 0., 2.], [-1., 0., 1.]])
        _SOBEL = torch.stack([kx, kx.T])[:, None].to(logit.device)
    p = torch.sigmoid(logit)
    gp = F.conv2d(p, _SOBEL.to(p.dtype), padding=1).abs().sum(1, keepdim=True)
    gy = F.conv2d(y, _SOBEL.to(y.dtype), padding=1).abs().sum(1, keepdim=True)
    return F.l1_loss(gp, gy)


def label_edges(y, width=2):
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
        pw = torch.tensor([10.0], device=logit.device, dtype=logit.dtype)
        loss = loss + w_edge * F.binary_cross_entropy_with_logits(edge_logit, et, pos_weight=pw)
    return loss, bce.detach()


def boundary_f1(pred, truth, tol=2):
    """How well the two outlines agree within `tol` px. Returns (f1, n_pred, n_true)."""
    pe = label_edges(pred.float(), 1) > 0.5
    te = label_edges(truth.float(), 1) > 0.5
    k = 2 * tol + 1
    pe_d = F.max_pool2d(pe.float(), k, 1, tol) > 0.5
    te_d = F.max_pool2d(te.float(), k, 1, tol) > 0.5
    tp_p = (pe & te_d).sum().item()
    tp_t = (te & pe_d).sum().item()
    np_, nt = pe.sum().item(), te.sum().item()
    prec = tp_p / np_ if np_ else 0.0
    rec = tp_t / nt if nt else 0.0
    return ((2 * prec * rec / (prec + rec)) if prec + rec else 0.0), np_, nt


# ───────────────────────────── model ─────────────────────────────
class ShardedRoadNet(nn.Module):
    def __init__(self, backbone, devices, taps=None, ch=256):
        super().__init__()
        import timm
        print("[model] building %s - loads fp32 on CPU first, so the 7B needs "
              "~27 GB RAM transiently before the bf16 cast halves it" % backbone, flush=True)
        t0 = time.time()
        self.enc = timm.create_model(backbone, pretrained=True, num_classes=0)
        self.dim = self.enc.embed_dim
        n = len(self.enc.blocks)
        self.taps = list(taps) if taps else [n // 4 - 1, n // 2 - 1, 3 * n // 4 - 1, n - 1]
        print("[model] %d blocks, dim %d, taps %s (%.0fs)"
              % (n, self.dim, self.taps, time.time() - t0), flush=True)

        self.enc.eval()
        for p in self.enc.parameters():
            p.requires_grad_(False)
        self.enc.to(torch.bfloat16)

        self.head = SharpDecoder(self.dim, ch)
        self.cls = nn.Conv2d(self.head.out_ch, 1, 1)
        self.edge = nn.Conv2d(self.head.out_ch, 1, 1)

        self.block_dev = shardmod.shard_encoder(self.enc, devices)
        self.out_dev = self.block_dev[-1]
        for m in (self.head, self.cls, self.edge):
            m.to(self.out_dev)
        enc_b = sum(p.numel() * p.element_size() for p in self.enc.parameters())
        dec_n = sum(p.numel() for p in self.head.parameters())
        print("[model] encoder %.2f GiB bf16 frozen | decoder %.2f M trainable | out_dev %s"
              % (enc_b / 2**30, dec_n / 1e6, self.out_dev), flush=True)

    def forward(self, x):
        with torch.no_grad():
            feats = shardmod.sharded_intermediates(
                self.enc, x.to(torch.bfloat16), self.block_dev, self.taps,
                norm=True, out_fmt="NCHW")
        feats = [f.float() for f in feats]
        f = self.head(feats, x.to(self.out_dev))
        logit, edge = self.cls(f), self.edge(f)
        return (logit, edge) if self.training else logit


@torch.no_grad()
def evaluate(model, loader, out_dev, thr=0.5):
    model.eval()
    tp = fp = fn = 0.0
    bf = []
    for x, y in loader:
        y = y.to(out_dev, non_blocking=True)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            logit = model(x)
        p = (torch.sigmoid(logit.float()) > thr).float()
        tp += (p * y).sum().item()
        fp += (p * (1 - y)).sum().item()
        fn += ((1 - p) * y).sum().item()
        f1b, npx, ntx = boundary_f1(p, y)
        if npx and ntx:
            bf.append(f1b)
    pr = tp / max(tp + fp, 1e-9)
    rc = tp / max(tp + fn, 1e-9)
    return {"precision": pr, "recall": rc,
            "f1": 2 * pr * rc / max(pr + rc, 1e-9),
            "iou": tp / max(tp + fp + fn, 1e-9),
            "boundary_f1": float(np.mean(bf)) if bf else 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--backbone", default="vit_7b_patch16_dinov3.sat493m")
    ap.add_argument("--devices", default="cuda:0,cuda:1")
    ap.add_argument("--out", default="runs/vit7b")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--accum", type=int, default=12,
                    help="batch x accum should land near the 48 the ViT-L run used")
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--wd", type=float, default=0.01)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--w-bnd", type=float, default=0.5)
    ap.add_argument("--w-edge", type=float, default=0.5)
    ap.add_argument("--taps", default="")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    torch.manual_seed(a.seed); np.random.seed(a.seed)
    devices = [d.strip() for d in a.devices.split(",") if d.strip()]
    taps = [int(t) for t in a.taps.split(",")] if a.taps else None

    tag = "sat493m" if "sat493m" in a.backbone else "lvd1689m"
    mean, std = STATS[tag]
    print("backbone %s | normalisation %s | devices %s" % (a.backbone, tag, devices), flush=True)

    tr = ChipSet(a.data, "train", mean, std, train=True)
    va = ChipSet(a.data, "val", mean, std, train=False)
    trl = DataLoader(tr, batch_size=a.batch, shuffle=True, num_workers=a.workers,
                     pin_memory=True, drop_last=True, persistent_workers=a.workers > 0)
    val = DataLoader(va, batch_size=a.batch, shuffle=False,
                     num_workers=max(2, a.workers // 2), pin_memory=True)
    print("train %d chips | val %d chips" % (len(tr), len(va)), flush=True)

    model = ShardedRoadNet(a.backbone, devices, taps=taps)
    out_dev = model.out_dev
    params = [p for n_, p in model.named_parameters()
              if p.requires_grad and not n_.startswith("enc.")]
    tot = sum(p.numel() for p in model.parameters())
    trn = sum(p.numel() for p in params)
    print("parameters: %.0f M total, %.2f M trainable (%.4f%%)"
          % (tot / 1e6, trn / 1e6, 100 * trn / tot), flush=True)

    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=a.wd)
    steps = max(1, len(trl) // a.accum) * a.epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=steps, pct_start=0.1)
    # Road is a small share of pixels; nudge BCE towards recall rather than
    # letting it win by predicting background everywhere.
    pw = torch.tensor([3.0], device=out_dev)

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "args.json").write_text(json.dumps(vars(a), indent=1), encoding="utf-8")

    best, hist = -1.0, []
    for ep in range(a.epochs):
        model.train(); model.enc.eval()      # frozen norms stay fixed
        t0, run = time.time(), 0.0
        opt.zero_grad(set_to_none=True)
        for i, (x, y) in enumerate(trl):
            y = y.to(out_dev, non_blocking=True)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                logit, edge = model(x)
                loss, _ = total_loss(logit.float(), y, edge_logit=edge.float(),
                                     w_bnd=a.w_bnd, w_edge=a.w_edge, pos_weight=pw)
            (loss / a.accum).backward()
            if (i + 1) % a.accum == 0:
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step(); opt.zero_grad(set_to_none=True)
                if sched.last_epoch < steps - 1:
                    sched.step()
            run += loss.item()
            if ep == 0 and i == 0:
                for d in devices:
                    if d.startswith("cuda"):
                        k = int(d.split(":")[1])
                        print("[mem] %s peak %.2f GiB after one step"
                              % (d, torch.cuda.max_memory_allocated(k) / 2**30), flush=True)
        m = evaluate(model, val, out_dev)
        hist.append({"epoch": ep, "loss": run / max(1, len(trl)), **m})
        print("ep %3d  loss %.4f  val F1 %.4f  P %.3f  R %.3f  IoU %.4f  bF1 %.4f  %.0fs"
              % (ep, run / max(1, len(trl)), m["f1"], m["precision"], m["recall"],
                 m["iou"], m["boundary_f1"], time.time() - t0), flush=True)
        (outdir / "history.json").write_text(json.dumps(hist, indent=1), encoding="utf-8")
        if m["f1"] > best:
            best = m["f1"]
            # The frozen backbone is byte-identical to the public timm weights,
            # so only the trained parts are saved: ~35 MB, not 13 GB.
            sd = {k: v for k, v in model.state_dict().items() if not k.startswith("enc.")}
            torch.save({"model": sd, "epoch": ep, "val_f1": best,
                        "boundary_f1": m["boundary_f1"], "decoder": "sharp",
                        "backbone": a.backbone, "taps": model.taps,
                        "frozen_backbone": True}, outdir / "best.pth")
            print("        saved best.pth (F1 %.4f)" % best, flush=True)
    print("\nbest val F1 %.4f -> %s" % (best, outdir / "best.pth"), flush=True)


if __name__ == "__main__":
    main()
