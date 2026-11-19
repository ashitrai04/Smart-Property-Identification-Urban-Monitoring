"""Train the SharpDecoder on a DINOv3 ViT-7B backbone split across two GPUs.

Deliberately a separate file from train_roads.py. That script trains the
ViT-L run that is in production at F1 0.837, and the way to not break a
working thing is to not edit it. Everything reusable is imported from it, so
the dataset, the losses, the metric and the checkpoint format are the same
code, not copies that can drift.

What is different here, and only this:

  * The backbone is 6.716 B parameters. In fp32 that is 26.9 GB, which does
    not fit a 40 GB card that other jobs are already using. It is held in
    bf16 (12.5 GiB) - lossless in practice because it is frozen and every
    forward runs under bf16 autocast anyway.

  * Even at 12.5 GiB it does not fit ONE card when ~14 GB is free, so the 40
    blocks are split across both. This is pipeline parallelism, not DDP: DDP
    puts a full copy on each card and would fix nothing. Sharding gives
    ~6.3 GiB of weights per card and ~7.7 GB of headroom for activations.
    See shard.py.

  * Only the decoder trains, so nothing backpropagates across the device
    boundary and there is no pipeline schedule to get wrong.

The checkpoint it writes carries backbone / decoder / taps / frozen_backbone
exactly as train_roads.py does, so gpu-server's roadnet_sharp.build() rebuilds
it without knowing it came from here.

Run:
  python train_roads_7b.py --data /path/ongole_roads_v2 \
      --devices cuda:0,cuda:1 --batch 2 --accum 24 --epochs 60 \
      --out runs/$(date +%Y%m%d-%H%M)-vit7b
"""
import argparse, json, os, sys, time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import shard as shardmod
from train_roads import ChipSet, STATS, total_loss, boundary_f1


class ShardedRoadNet(nn.Module):
    def __init__(self, backbone, devices, taps=None, ch=256, decoder="sharp"):
        super().__init__()
        import timm
        from decoders import DECODERS

        print("[model] building %s (this loads fp32 on CPU first - needs ~27 GB RAM "
              "for the 7B, then halves)" % backbone, flush=True)
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

        self.frozen = True
        self.decoder_name = decoder
        if decoder not in DECODERS:
            raise ValueError("decoder must be one of %s" % list(DECODERS))
        self.head = DECODERS[decoder](self.dim, ch)
        if decoder == "uper":
            raise SystemExit("this script supports the sharp decoder only; "
                             "uper needs the detail/mix branch from train_roads.py")
        self.detail = self.mix = None
        self.cls = nn.Conv2d(self.head.out_ch, 1, 1)
        self.edge = nn.Conv2d(self.head.out_ch, 1, 1)

        self.block_dev = shardmod.shard_encoder(self.enc, devices)
        self.out_dev = self.block_dev[-1]
        for m in (self.head, self.cls, self.edge):
            m.to(self.out_dev)
        enc_b = sum(p.numel() * p.element_size() for p in self.enc.parameters())
        dec_b = sum(p.numel() * p.element_size() for p in self.head.parameters())
        print("[model] encoder %.2f GiB bf16 (frozen) | decoder %.1f MB trainable"
              % (enc_b / 2**30, dec_b / 1e6), flush=True)

    def forward(self, x):
        with torch.no_grad():
            feats = shardmod.sharded_intermediates(
                self.enc, x.to(torch.bfloat16), self.block_dev, self.taps,
                norm=True, out_fmt="NCHW")
        feats = [f.float() for f in feats]
        xo = x.to(self.out_dev)
        f = self.head(feats, xo)
        logit, edge = self.cls(f), self.edge(f)
        return (logit, edge) if self.training else logit


@torch.no_grad()
def evaluate(model, loader, out_dev, thr=0.5):
    model.eval()
    tp = fp = fn = 0.0
    bf1 = []
    for x, y in loader:
        y = y.to(out_dev, non_blocking=True)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            logit = model(x)
        p = (torch.sigmoid(logit.float()) > thr).float()
        tp += (p * y).sum().item()
        fp += (p * (1 - y)).sum().item()
        fn += ((1 - p) * y).sum().item()
        bf1.append(boundary_f1(p, y))
    pr = tp / max(tp + fp, 1e-9)
    rc = tp / max(tp + fn, 1e-9)
    return {"precision": pr, "recall": rc,
            "f1": 2 * pr * rc / max(pr + rc, 1e-9),
            "iou": tp / max(tp + fp + fn, 1e-9),
            "boundary_f1": float(np.mean([b for b in bf1 if b == b])) if bf1 else 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--backbone", default="vit_7b_patch16_dinov3.sat493m")
    ap.add_argument("--devices", default="cuda:0,cuda:1",
                    help="comma separated; the backbone is split across these")
    ap.add_argument("--out", default="runs/vit7b")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--accum", type=int, default=24,
                    help="batch x accum should land near the 48 the ViT-L run used")
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--wd", type=float, default=0.01)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--decoder", default="sharp")
    ap.add_argument("--w-bnd", type=float, default=0.5)
    ap.add_argument("--w-edge", type=float, default=0.5)
    ap.add_argument("--taps", default="", help="comma separated; default is evenly spaced")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    np.random.seed(a.seed)
    devices = [d.strip() for d in a.devices.split(",") if d.strip()]
    taps = [int(t) for t in a.taps.split(",")] if a.taps else None

    tag = "sat493m" if "sat493m" in a.backbone else "lvd1689m"
    mean, std = STATS[tag]
    print("backbone %s  normalisation %s" % (a.backbone, tag), flush=True)

    tr = ChipSet(a.data, "train", mean, std, train=True)
    va = ChipSet(a.data, "val", mean, std, train=False)
    trl = DataLoader(tr, batch_size=a.batch, shuffle=True, num_workers=a.workers,
                     pin_memory=True, drop_last=True, persistent_workers=a.workers > 0)
    val = DataLoader(va, batch_size=a.batch, shuffle=False,
                     num_workers=max(2, a.workers // 2), pin_memory=True)
    print("train %d chips   val %d chips" % (len(tr), len(va)), flush=True)

    model = ShardedRoadNet(a.backbone, devices, taps=taps, decoder=a.decoder)
    out_dev = model.out_dev
    params = [p for n_, p in model.named_parameters()
              if p.requires_grad and not n_.startswith("enc.")]
    tot = sum(p.numel() for p in model.parameters())
    trn = sum(p.numel() for p in params)
    print("parameters: %.0f M total, %.1f M trainable (%.3f%%)"
          % (tot / 1e6, trn / 1e6, 100 * trn / tot), flush=True)

    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=a.wd)
    steps = max(1, len(trl) // a.accum) * a.epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr,
                                                total_steps=steps, pct_start=0.1)
    pw = torch.tensor([3.0], device=out_dev)

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "args.json").write_text(json.dumps(vars(a), indent=1), encoding="utf-8")

    best, hist = -1.0, []
    for ep in range(a.epochs):
        model.train()
        model.enc.eval()                      # frozen norms stay fixed
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
                opt.step()
                opt.zero_grad(set_to_none=True)
                if sched.last_epoch < steps - 1:
                    sched.step()
            run += loss.item()
            if i == 0 and ep == 0:
                for d in devices:
                    if d.startswith("cuda"):
                        k = int(d.split(":")[1])
                        print("[mem] %s peak %.2f GiB" % (d, torch.cuda.max_memory_allocated(k) / 2**30),
                              flush=True)
        m = evaluate(model, val, out_dev)
        hist.append({"epoch": ep, "loss": run / max(1, len(trl)), **m})
        print("ep %3d  loss %.4f  val F1 %.4f  P %.3f  R %.3f  IoU %.4f  bF1 %.4f  %.0fs"
              % (ep, run / max(1, len(trl)), m["f1"], m["precision"], m["recall"],
                 m["iou"], m["boundary_f1"], time.time() - t0), flush=True)
        (outdir / "history.json").write_text(json.dumps(hist, indent=1), encoding="utf-8")
        if m["f1"] > best:
            best = m["f1"]
            sd = {k: v for k, v in model.state_dict().items() if not k.startswith("enc.")}
            torch.save({"model": sd, "epoch": ep, "val_f1": best,
                        "boundary_f1": m["boundary_f1"], "decoder": a.decoder,
                        "backbone": a.backbone, "taps": model.taps,
                        "frozen_backbone": True}, outdir / "best.pth")
            print("        saved best.pth (F1 %.4f)" % best, flush=True)
    print("\nbest val F1 %.4f -> %s" % (best, outdir / "best.pth"), flush=True)


if __name__ == "__main__":
    main()
