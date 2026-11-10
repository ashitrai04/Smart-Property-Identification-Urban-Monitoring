"""Loader for the checkpoints trained on PARAM Rudra.

These are a different network from the one in roadnet_arch.py, and the
difference is the point of them:

    roadnet_arch.py   DINOv3 ViT-L + UPerNet + a two-conv detail branch.
                      Reconstructed from a shipped checkpoint; 439 tensors,
                      encoder included, 632 MB.
    this one          DINOv3 ViT-L (or 7B) + SharpDecoder: a convolutional
                      stem reading the image at strides 1/2/4/8, fused into
                      the token pyramid on the way up, with an auxiliary edge
                      head supervised during training.

The practical difference is the file. A PARAM checkpoint is ~34 MB because the
frozen backbone is left out - it is byte-identical to the public timm weights,
so shipping it again would be 1.2 GB of duplication. That means loading one
needs the public backbone available, from the HF cache under
gpu-server/weights/hf, and a machine with no cache and no network cannot load
these at all. It says so plainly rather than failing deep inside timm.

Nothing here guesses at the architecture: train_roads.py writes `decoder`,
`backbone`, `taps` and `frozen_backbone` into every checkpoint, so the network
is rebuilt from what the file says it is. A checkpoint that does not carry
those keys is not one of these, and the caller should use roadnet_arch.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ML = os.path.join(REPO, "ml", "dinov3-roads")

# The training code is the reference implementation of this architecture, and
# importing it is better than keeping a second copy here that drifts from it.
# If the ml/ tree is not shipped alongside - a slimmed deployment, say - this
# is where that becomes visible.
if ML not in sys.path:
    sys.path.insert(0, ML)


def is_param_checkpoint(ck):
    """Does this checkpoint describe a SharpDecoder network?"""
    return isinstance(ck, dict) and "decoder" in ck and "backbone" in ck


def describe(ck):
    """What the file says about itself, for logs and /health."""
    return {k: ck.get(k) for k in
            ("backbone", "decoder", "taps", "epoch", "val_f1",
             "boundary_f1", "frozen_backbone") if k in ck}


def build(ck):
    """Rebuild the network this checkpoint was trained as, and load it.

    Returns (model, missing, unexpected) like roadnet_arch.verify, so the
    caller's strict-load check is the same for both architectures.
    """
    try:
        import train_roads as tr
    except ModuleNotFoundError as e:
        raise RuntimeError(
            f"this checkpoint needs the training code in ml/dinov3-roads, "
            f"which is not importable from here ({ML}): {e}") from e

    backbone = ck.get("backbone") or "vit_large_patch16_dinov3.sat493m"
    decoder = ck.get("decoder") or "sharp"
    taps = ck.get("taps")
    frozen = bool(ck.get("frozen_backbone", True))

    # The checkpoint omits the frozen backbone, so it has to come from timm -
    # from the local HF cache if it is there, from the network if not. A
    # machine with neither fails here, with a message that says what to do,
    # rather than inside timm with a connection error.
    try:
        model = tr.RoadNet(backbone, taps=taps, freeze=frozen, decoder=decoder)
    except Exception as e:
        raise RuntimeError(
            f"could not build {backbone} with the '{decoder}' decoder. This "
            f"checkpoint does not carry the backbone - it is the public timm "
            f"weights - so they must be in the HF cache "
            f"({os.environ.get('HF_HOME', '<unset>')}) or downloadable. "
            f"Underlying error: {type(e).__name__}: {str(e)[:200]}") from e

    sd = ck.get("model", ck)
    missing, unexpected = model.load_state_dict(sd, strict=False)
    # The backbone keys are MEANT to be missing - they were never saved. Only
    # a decoder key going missing is a real mismatch, and that is what the
    # caller must refuse to run.
    missing = [k for k in missing if not k.startswith("enc.")]
    return model, list(missing), list(unexpected)
