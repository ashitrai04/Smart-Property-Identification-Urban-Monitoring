"""Split a frozen DINOv3 encoder across several GPUs.

Why this exists: the 7B backbone is 12.5 GiB in bf16, and on a shared box
where other jobs hold most of each card there may be ~14 GB free per GPU.
Resident on one card that leaves ~1.5 GB for everything else, which is not a
training run, it is a coin toss. Split across two cards it is ~6.3 GiB each
with ~7.7 GB of headroom, which is comfortable.

This is pipeline (model) parallelism, not DDP. DDP puts a FULL copy on every
card and so does nothing for memory - it buys throughput you cannot spend if
the model does not fit. With two cards you can have one or the other, and
when the model barely fits, fitting wins.

The backbone is frozen, which is what makes this nearly free: no gradients
cross the device boundary, so there is no backward pipeline to schedule and
no activation stashing. The only cost is moving one token tensor between
cards once per boundary - at 512 px that is 1029 x 4096 x 2 bytes = 8.4 MB,
microseconds over NVLink.

timm's own forward_intermediates cannot be used here. Its loop computes the
rotary position embedding once and passes that same tensor to every block,
and applies self.norm to every tap, so with blocks on different devices those
tensors arrive on the wrong card. The loop below is that loop, with the
placement made explicit.
"""
import torch


def shard_encoder(enc, devices, verbose=True):
    """Place `enc` across `devices`, balanced by parameter count.

    Returns the per-block device list. The stem goes with the first block and
    `enc.norm` with the last, so a tap taken from any block is normed on the
    device it already sits on.
    """
    devices = [torch.device(d) for d in devices]
    blocks = enc.blocks
    n = len(blocks)
    if len(devices) == 1:
        enc.to(devices[0])
        return [devices[0]] * n

    # Balance by parameters rather than block count: the blocks are uniform in
    # DINOv3, but this stays correct if a future backbone's are not.
    sizes = [sum(p.numel() for p in b.parameters()) for b in blocks]
    total = sum(sizes)
    target = total / len(devices)
    block_dev, cur, acc, di = [], devices[0], 0.0, 0
    for i, s in enumerate(sizes):
        # Move on once this device has its share and blocks remain for the rest.
        if di < len(devices) - 1 and acc >= target * (di + 1) and (n - i) > (len(devices) - di - 1):
            di += 1
            cur = devices[di]
        block_dev.append(cur)
        acc += s

    for b, d in zip(blocks, block_dev):
        b.to(d)
    first, last = block_dev[0], block_dev[-1]
    for name in ("patch_embed", "pos_embed", "rope", "cls_token", "reg_token",
                 "norm_pre", "pos_drop", "patch_drop"):
        m = getattr(enc, name, None)
        if m is None:
            continue
        if isinstance(m, torch.nn.Parameter):
            setattr(enc, name, torch.nn.Parameter(m.data.to(first), requires_grad=m.requires_grad))
        elif hasattr(m, "to"):
            m.to(first)
    if getattr(enc, "norm", None) is not None:
        enc.norm.to(last)

    if verbose:
        per = {}
        for b, d in zip(blocks, block_dev):
            per[str(d)] = per.get(str(d), 0) + sum(p.numel() * p.element_size() for p in b.parameters())
        print("[shard] %d blocks over %s" % (n, [str(d) for d in devices]))
        for d in devices:
            k = str(d)
            cnt = sum(1 for x in block_dev if str(x) == k)
            print("[shard]   %-10s %2d blocks  %.2f GiB" % (k, cnt, per.get(k, 0) / 2**30))
    return block_dev


def sharded_intermediates(enc, x, block_dev, taps, norm=True, out_fmt="NCHW"):
    """timm's Eva.forward_intermediates, with device placement made explicit.

    Returns the taps as NCHW feature maps on the device of `enc.norm` (the
    last shard), ready for a decoder living there.
    """
    taps = set(int(t) for t in taps)
    first = block_dev[0]
    x = x.to(first)

    x = enc.patch_embed(x)
    x, rope = enc._pos_embed(x)
    if getattr(enc, "norm_pre", None) is not None:
        x = enc.norm_pre(x)

    # One copy of the rotary embedding per device, made once. Moving it inside
    # the loop would copy it 40 times a step for no reason.
    mixed = bool(getattr(enc, "rope_mixed", False))
    rope_by_dev = {}
    if rope is not None:
        for d in {str(v) for v in block_dev}:
            rope_by_dev[d] = ([r.to(d) for r in rope] if mixed else rope.to(d))

    out = {}
    cur = first
    for i, blk in enumerate(enc.blocks):
        d = block_dev[i]
        if d != cur:
            x = x.to(d)
            cur = d
        r = rope_by_dev.get(str(d))
        if mixed and r is not None:
            r = r[i]
        x = blk(x, rope=r) if r is not None else blk(x)
        if i in taps:
            out[i] = x

    nd = next(enc.norm.parameters()).device if getattr(enc, "norm", None) is not None else cur
    feats = []
    for i in sorted(out):
        t = out[i].to(nd)
        if norm and getattr(enc, "norm", None) is not None:
            t = enc.norm(t)
        feats.append(t)

    # Drop prefix tokens (cls + registers) and reshape to NCHW, as timm does.
    npfx = getattr(enc, "num_prefix_tokens", 0)
    if out_fmt == "NCHW":
        B, _, C = feats[0].shape
        H = W = None
        side = int((feats[0].shape[1] - npfx) ** 0.5)
        H = W = side
        feats = [f[:, npfx:].reshape(B, H, W, C).permute(0, 3, 1, 2).contiguous()
                 for f in feats]
    else:
        feats = [f[:, npfx:] for f in feats]
    return feats
