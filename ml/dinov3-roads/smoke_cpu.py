"""Exercise the whole training path on a CPU, with no GPU and no downloads.

Why this exists: `sbatch --test-only` says the gpu partition is five days out,
and it reports the same start time whatever you ask for, so there is no
backfill gap to aim at. Five days is a long time to wait to discover that the
dataloader pairs the wrong mask, or that the decoder will not build, or that
the checkpoint cannot be read back. train_roads.py is CUDA-only - device is
torch.device("cuda", ...) - so it cannot be used to find that out.

This runs the same pieces on a CPU: the dataset, the model, the loss, the
metrics and a checkpoint round trip. It will not tell you anything about
speed or memory - preflight.py does that, on a GPU - but it will tell you
whether the code is correct, today, on either the cpu partition (which starts
immediately) or a laptop.

    python smoke_cpu.py --data /scratch/$USER/ongole_roads_v2
    python smoke_cpu.py --data C:/ongole_roads_v2 --backbone vit_small_patch16_dinov3

--random-weights builds the backbone without pretrained weights, so it needs
no network and no HF cache. The architecture, shapes and gradients are
identical; only the values differ, which is irrelevant to the question being
asked here.
"""
import argparse
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAIL = []


def check(name, fn):
    try:
        detail = fn()
        print(f"  ok    {name}" + (f" - {detail}" if detail else ""))
        return True
    except Exception as e:
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        FAIL.append(name)
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--backbone", default="vit_small_patch16_dinov3.lvd1689m")
    ap.add_argument("--decoder", default="sharp", choices=["sharp", "uper"])
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--chip", type=int, default=512)
    ap.add_argument("--random-weights", action="store_true", default=True)
    ap.add_argument("--pretrained", dest="random_weights", action="store_false",
                    help="use the real cached weights instead of random ones")
    a = ap.parse_args()

    torch.manual_seed(0)
    print(f"torch {torch.__version__}  cpu  backbone {a.backbone}  decoder {a.decoder}")
    print(f"weights: {'random (no download)' if a.random_weights else 'pretrained'}\n")

    import train_roads as tr

    # ---------- 1. the dataset ----------
    print("dataset")
    root = Path(a.data)
    ds = {}

    # ChipSet needs the normalisation stats, and they are not ImageNet's:
    # the sat493m weights were trained with their own, and feeding ImageNet
    # numbers to a satellite-pretrained backbone quietly costs accuracy.
    tag = "sat493m" if "sat493m" in a.backbone else "lvd1689m"
    mean, std = tr.STATS[tag]
    print(f"  normalisation: {tag} mean={mean} std={std}")

    def _load(split):
        d = tr.ChipSet(root, split, mean, std, train=False)
        ds[split] = d
        return f"{len(d)} chips"

    for split in ("train", "val", "test"):
        check(f"{split} split loads", lambda s=split: _load(s))

    def _pair():
        d = ds["train"]
        x, y = d[0]
        assert x.ndim == 3 and x.shape[0] == 3, f"image is {tuple(x.shape)}, want (3,H,W)"
        assert y.shape[-2:] == x.shape[-2:], f"mask {tuple(y.shape)} != image {tuple(x.shape)}"
        assert x.dtype == torch.float32, f"image dtype {x.dtype}"
        lo, hi = float(x.min()), float(x.max())
        assert -10 < lo and hi < 10, f"image range [{lo:.2f},{hi:.2f}] - normalisation wrong?"
        vals = set(np.unique(y.numpy()).tolist())
        assert vals <= {0.0, 1.0}, f"mask holds {sorted(vals)[:5]}, want 0/1"
        return f"image {tuple(x.shape)} in [{lo:.2f},{hi:.2f}], mask {tuple(y.shape)}"
    check("image/mask pair is well formed", _pair)

    def _positives():
        # A sample with no road anywhere would train fine and teach nothing;
        # a sample that is ALL road means the mask is being read inverted.
        d = ds["train"]
        fr = [float(d[i][1].mean()) for i in range(0, min(len(d), 60))]
        m = sum(fr) / len(fr)
        assert 0.005 < m < 0.60, f"mean road fraction {m:.3f} over 60 chips is implausible"
        return f"mean road fraction {m:.3f} over {len(fr)} chips"
    check("masks carry a sane amount of road", _positives)

    def _names():
        # The pairing is by stem. If images and masks ever diverge, every
        # metric still computes and every number is meaningless.
        d = ds["train"]
        stems_i = {p.stem for p in list(d.img)[:200]}
        missing = [s for s in stems_i if not (d.msk / f"{s}.png").exists()]
        assert not missing, f"{len(missing)} images have no mask, e.g. {missing[:3]}"
        return f"200 sampled images all have their mask"
    check("every image has its mask", _names)

    # ---------- 2. the model ----------
    print("\nmodel")
    model = [None]

    def _build():
        import timm
        from decoders import DECODERS
        real = timm.create_model
        if a.random_weights:
            timm.create_model = lambda *ar, **kw: real(*ar, **{**kw, "pretrained": False})
        try:
            model[0] = tr.RoadNet(a.backbone, decoder=a.decoder, freeze=True)
        finally:
            timm.create_model = real
        m = model[0]
        tot = sum(p.numel() for p in m.parameters())
        trn = sum(p.numel() for p in m.parameters() if p.requires_grad)
        assert trn > 0, "nothing is trainable - the decoder is frozen too"
        assert trn < tot, "the backbone is not frozen"
        return (f"{tot/1e6:.0f} M total, {trn/1e6:.1f} M trainable "
                f"({100*trn/tot:.1f}%), embed_dim {m.dim}, taps {m.taps}")
    if not check("builds, backbone frozen", _build):
        print("\ncannot continue without a model")
        sys.exit(1)

    def _forward():
        m = model[0].eval()
        x = torch.randn(1, 3, a.chip, a.chip)
        with torch.no_grad():
            out = m(x)
        logit = out[0] if isinstance(out, (tuple, list)) else out
        assert logit.shape[-2:] == (a.chip, a.chip), \
            f"output {tuple(logit.shape)} is not full resolution - an upsample is missing"
        assert logit.shape[1] == 1, f"{logit.shape[1]} channels, want 1"
        return f"{tuple(x.shape)} -> {tuple(logit.shape)}, full resolution"
    check("forward returns a full-resolution logit", _forward)

    def _train_step():
        m = model[0].train()
        x = torch.randn(a.batch, 3, a.chip, a.chip)
        y = (torch.rand(a.batch, 1, a.chip, a.chip) > 0.92).float()
        out = m(x)
        logit, edge = (out if isinstance(out, (tuple, list)) else (out, None))
        loss = tr.total_loss(logit, y, edge_logit=edge)
        loss = loss[0] if isinstance(loss, (tuple, list)) else loss
        assert torch.isfinite(loss), f"loss is {loss.item()}"
        loss.backward()
        grads = [p for p in m.parameters() if p.requires_grad and p.grad is not None]
        assert grads, "backward produced no gradients on any trainable parameter"
        gn = sum(float(p.grad.norm()) for p in grads)
        assert np.isfinite(gn) and gn > 0, f"gradient norm is {gn}"
        enc_g = [p for p in m.enc.parameters() if p.grad is not None]
        assert not enc_g, f"{len(enc_g)} frozen backbone parameters got gradients"
        return (f"loss {float(loss):.4f}, grad norm {gn:.3f} over "
                f"{len(grads)} tensors, backbone clean")
    check("loss and backward, edge head included", _train_step)

    # ---------- 3. the metrics ----------
    print("\nmetrics")

    def _perfect():
        # A perfect prediction must score 1.0. If it does not, the metric is
        # wrong and every number the run reports is wrong with it.
        y = (torch.rand(1, 1, 128, 128) > 0.9).float()
        # boundary_f1 takes a binary prediction, not a logit, and returns
        # (f1, n_pred_edge, n_true_edge).
        f1, npx, ntx = tr.boundary_f1(y.clone(), y)
        assert abs(f1 - 1.0) < 1e-6, f"a perfect prediction scored {f1}"
        half = y.clone(); half[:, :, :64, :] = 0
        f1b, _, _ = tr.boundary_f1(half, y)
        assert f1b < f1, f"blanking half the image did not lower the score ({f1b} vs {f1})"
        return f"perfect = {f1:.4f}, half blanked = {f1b:.4f}, {ntx} true edge px"
    check("boundary_f1 scores a perfect prediction", _perfect)

    def _edges():
        y = torch.zeros(1, 1, 64, 64)
        y[:, :, 20:40, 20:40] = 1.0
        e = tr.label_edges(y)
        n = int((e > 0.5).sum())
        assert n > 0, "a solid square has no edge"
        assert n < int(y.sum()), "the 'edge' is the whole square"
        return f"a 20x20 square has {n} edge pixels, interior excluded"
    check("label_edges finds a boundary, not an area", _edges)

    # ---------- 4. the checkpoint ----------
    print("\ncheckpoint")

    def _roundtrip():
        m = model[0]
        sd = {k: v for k, v in m.state_dict().items() if not k.startswith("enc.")}
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "best.pth"
            torch.save({"model": sd, "backbone": a.backbone,
                        "decoder": a.decoder, "val_f1": 0.0}, p)
            mb = p.stat().st_size / 1e6
            ck = torch.load(p, map_location="cpu", weights_only=False)
            miss, unexp = m.load_state_dict(ck["model"], strict=False)
            enc_missing = [k for k in miss if k.startswith("enc.")]
            assert not unexp, f"unexpected keys on reload: {list(unexp)[:3]}"
            assert len(miss) == len(enc_missing), \
                f"decoder keys missing on reload: {[k for k in miss if not k.startswith('enc.')][:3]}"
        return (f"{mb:.1f} MB without the backbone, reloads with only the "
                f"{len(enc_missing)} frozen backbone keys absent")
    check("saves and reloads without the frozen backbone", _roundtrip)

    print()
    if FAIL:
        print(f"{len(FAIL)} CHECK(S) FAILED: {', '.join(FAIL)}")
        sys.exit(1)
    print("all checks passed - the training path is sound on this data.")
    print("Speed and memory are a separate question: preflight.py, on a GPU.")


if __name__ == "__main__":
    main()
