"""Prove the run will work before it reaches the queue.

A PARAM allocation that dies ten minutes in on a missing file, a wrong batch
size or an uncached checkpoint costs a queue slot and a day. Everything here
is the cheap version of what the real job does: build the model, push one
batch forward and backward, measure the peak memory it really used, and time
enough steps to estimate the walltime. Two minutes on a login node or a short
debug allocation, and it either prints a green summary with the batch size and
walltime to request, or it names exactly what is missing.

    python preflight.py --data $SCRATCH/ongole_roads \
        --backbone vit_7b_patch16_dinov3.sat493m --batch 2

Exit code is 0 only if every check passed, so it can gate a submission:

    python preflight.py ... && sbatch slurm_train.sh
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"
_fails, _warns = [], []


def ok(msg):
    print(OK + msg, flush=True)


def bad(msg):
    _fails.append(msg)
    print(BAD + " " + msg, flush=True)


def warn(msg):
    _warns.append(msg)
    print(WARN + " " + msg, flush=True)


def human_bytes(n):
    return f"{n/1e9:.1f} GB"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--backbone", default="vit_large_patch16_dinov3.sat493m")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--chip", type=int, default=512)
    ap.add_argument("--epochs", type=int, default=60, help="only used for the walltime estimate")
    ap.add_argument("--steps", type=int, default=6, help="timed steps")
    ap.add_argument("--unfreeze-last", type=int, default=0)
    a = ap.parse_args()

    print("=" * 72)
    print(f"preflight: {a.backbone}  batch {a.batch}  chip {a.chip}")
    print("=" * 72)

    # ── 1. interpreter and libraries ──────────────────────────────
    print("\n[1] environment")
    print(f"       python {sys.version.split()[0]}  ({sys.executable})")
    try:
        import torch
        ok(f"torch {torch.__version__}, cuda {torch.version.cuda}")
    except Exception as e:
        bad(f"torch will not import: {e}")
        return finish()
    try:
        import timm
        ok(f"timm {timm.__version__}")
    except Exception as e:
        bad(f"timm will not import: {e}")
        return finish()
    for mod in ("rasterio", "numpy", "PIL"):
        try:
            __import__(mod)
            ok(f"{mod}")
        except Exception as e:
            bad(f"{mod} missing: {e}")

    # ── 2. GPUs ───────────────────────────────────────────────────
    print("\n[2] gpu")
    if not torch.cuda.is_available():
        bad("no CUDA device visible. On a login node this is expected - rerun "
            "inside a debug allocation (salloc) before trusting the memory numbers.")
        return finish()
    n = torch.cuda.device_count()
    for i in range(n):
        p = torch.cuda.get_device_properties(i)
        ok(f"gpu{i}: {p.name}, {p.total_memory/1e9:.0f} GB, sm_{p.major}{p.minor}")
    total_mem = torch.cuda.get_device_properties(0).total_memory
    if torch.cuda.is_bf16_supported():
        ok("bf16 supported")
        amp = torch.bfloat16
    else:
        warn("bf16 NOT supported on this card; the script will need --amp fp16")
        amp = torch.float16

    # ── 3. weights cached ─────────────────────────────────────────
    print("\n[3] pretrained weights")
    print(f"       HF_HOME={os.environ.get('HF_HOME', '<default>')}  "
          f"HF_HUB_OFFLINE={os.environ.get('HF_HUB_OFFLINE', '0')}")
    known = set(timm.list_models("*dinov3*", pretrained=True))
    if a.backbone not in known:
        bad(f"'{a.backbone}' is not a known pretrained tag. Available:\n         "
            + "\n         ".join(sorted(known)))
        return finish()
    ok(f"'{a.backbone}' is a valid tag")

    # ── 4. dataset ────────────────────────────────────────────────
    print("\n[4] dataset")
    root = Path(a.data)
    counts = {}
    for split in ("train", "val", "test"):
        imgs = sorted((root / "images" / split).glob("*.tif"))
        counts[split] = len(imgs)
        if split in ("train", "val") and not imgs:
            bad(f"no chips in {root/'images'/split}")
        missing = [p.stem for p in imgs[:200]
                   if not (root / "masks" / split / f"{p.stem}.png").exists()]
        if missing:
            bad(f"{split}: {len(missing)} chips have no mask, e.g. {missing[:3]}")
        elif imgs:
            ok(f"{split}: {len(imgs):,} chips, masks present")
    if counts.get("train", 0) and counts.get("val", 0):
        ratio = counts["val"] / max(1, counts["train"])
        if ratio < 0.05:
            warn(f"val is only {100*ratio:.0f}% of train; scores will be noisy")
    free = shutil.disk_usage(root).free if root.exists() else 0
    print(f"       free space on the data volume: {human_bytes(free)}")

    # ── 5. build the model ────────────────────────────────────────
    print("\n[5] model")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tr", Path(__file__).resolve().parent / "train_roads.py")
    tr = importlib.util.module_from_spec(spec)
    sys.argv = [sys.argv[0]]
    spec.loader.exec_module(tr)

    t0 = time.time()
    try:
        model = tr.RoadNet(a.backbone, freeze=a.unfreeze_last == 0).cuda()
    except Exception as e:
        bad(f"model would not build: {type(e).__name__}: {str(e)[:200]}")
        return finish()
    if a.unfreeze_last:
        for b in model.enc.blocks[-a.unfreeze_last:]:
            for p in b.parameters():
                p.requires_grad_(True)
        model.frozen = False
    tot = sum(p.numel() for p in model.parameters())
    trn = sum(p.numel() for p in model.parameters() if p.requires_grad)
    ok(f"built in {time.time()-t0:.0f}s: {tot/1e6:,.0f} M params, "
       f"{trn/1e6:,.1f} M trainable ({100*trn/tot:.2f}%)")
    ok(f"taps {model.taps}, embed_dim {model.dim}, patch "
       f"{model.enc.patch_embed.patch_size[0]} -> {a.chip//model.enc.patch_embed.patch_size[0]}"
       f"x{a.chip//model.enc.patch_embed.patch_size[0]} tokens")

    # ── 6. one real step, and what it costs ───────────────────────
    print("\n[6] one training step")
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1e-4)
    x = torch.randn(a.batch, 3, a.chip, a.chip, device="cuda")
    y = (torch.rand(a.batch, 1, a.chip, a.chip, device="cuda") > 0.95).float()
    torch.cuda.reset_peak_memory_stats()
    try:
        for i in range(a.steps):
            if i == 1:
                torch.cuda.synchronize(); t0 = time.time()
            with torch.autocast("cuda", dtype=amp):
                logit = model(x)
                loss, _ = tr.total_loss(logit.float(), y)
            loss.backward()
            opt.step(); opt.zero_grad(set_to_none=True)
        torch.cuda.synchronize()
    except torch.cuda.OutOfMemoryError:
        peak = torch.cuda.max_memory_allocated()
        bad(f"OUT OF MEMORY at batch {a.batch} (peak {human_bytes(peak)} of "
            f"{human_bytes(total_mem)}). Halve --batch, or add --grad-ckpt, "
            f"and raise --accum to keep the effective batch the same.")
        return finish()
    except Exception as e:
        bad(f"training step failed: {type(e).__name__}: {str(e)[:200]}")
        return finish()
    dt = (time.time() - t0) / max(1, a.steps - 1)
    peak = torch.cuda.max_memory_allocated()
    ok(f"loss {float(loss):.4f}, {dt*1000:.0f} ms/step, peak {human_bytes(peak)} "
       f"of {human_bytes(total_mem)} ({100*peak/total_mem:.0f}%)")
    if peak / total_mem > 0.90:
        warn("over 90% of the card at this batch; a slightly larger chip or an "
             "unlucky batch will OOM mid-run. Consider one step down.")
    else:
        head = int(a.batch * (0.85 * total_mem) / peak)
        if head > a.batch:
            ok(f"headroom suggests --batch {head} would still fit on one card")

    # ── 7. what to ask the scheduler for ──────────────────────────
    print("\n[7] walltime estimate")
    ntrain = counts.get("train", 0)
    if ntrain and dt:
        for gpus in (1, 2, 4):
            steps_ep = ntrain / (a.batch * gpus)
            hours = steps_ep * dt * a.epochs / 3600
            print(f"       {gpus} gpu(s): {steps_ep:,.0f} steps/epoch, "
                  f"~{hours:.1f} h for {a.epochs} epochs")
        print("       (add ~20% for dataloading, validation and checkpointing)")
    else:
        warn("no training chips, so no walltime estimate")

    return finish()


def finish():
    print("\n" + "=" * 72)
    if _fails:
        print(f"PREFLIGHT FAILED - {len(_fails)} problem(s). Do not submit:")
        for f in _fails:
            print("   - " + f.split("\n")[0])
        print("=" * 72)
        sys.exit(1)
    if _warns:
        print(f"preflight passed with {len(_warns)} warning(s):")
        for w in _warns:
            print("   - " + w)
    else:
        print("preflight passed. Safe to sbatch.")
    print("=" * 72)
    sys.exit(0)


if __name__ == "__main__":
    main()
