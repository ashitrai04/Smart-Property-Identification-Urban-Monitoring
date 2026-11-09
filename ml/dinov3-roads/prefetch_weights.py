"""Download the DINOv3 weights on the login node, before any job is queued.

Compute nodes on PARAM systems generally have no route to the internet. A
training script that calls timm.create_model(pretrained=True) there does not
fail quickly - it stalls on a connection it will never get, burns the
allocation, and dies on a timeout that says nothing useful. Running this once
on the login node puts the weights in $HF_HOME, and slurm_train.sh then sets
HF_HUB_OFFLINE=1 so a missing file is reported immediately instead.

    export HF_HOME=$SCRATCH/hf
    python prefetch_weights.py vit_large_patch16_dinov3.sat493m
    python prefetch_weights.py vit_7b_patch16_dinov3.sat493m     # 13 GB

Two different numbers, and they are easy to confuse - I confused them:

                                params   DOWNLOAD (fp32)   ON THE CARD (bf16)
    vit_small_patch16_dinov3      21 M        0.09 GB            0.04 GB
    vit_base_patch16_dinov3       86 M        0.34 GB            0.17 GB
    vit_large_patch16_dinov3     303 M        1.21 GB            0.61 GB
    vit_huge_plus_patch16_dinov3 840 M        3.36 GB            1.68 GB
    vit_7b_patch16_dinov3       6716 M       26.90 GB           13.43 GB

The checkpoints on the Hub are fp32, so the 7B is a **26.9 GB** download -
twice what you need in VRAM, because training loads it and casts to bf16.
Measured, not estimated: that is what the progress bar reported.

Budget /scratch accordingly. The full cache for ViT-L + 7B is about 28 GB
against a 200 GB quota, and the Hub's xet storage may hold a download blob
and the reconstructed file at once, so allow headroom while it runs.
"""
import os
import sys

DEFAULT = ["vit_large_patch16_dinov3.sat493m"]


def main():
    names = sys.argv[1:] or DEFAULT
    import timm
    import torch

    print(f"timm {timm.__version__}   HF_HOME={os.environ.get('HF_HOME', '<default>')}")
    known = set(timm.list_models("*dinov3*", pretrained=True))
    for n in names:
        if n not in known:
            print(f"\n{n}: NOT a known pretrained tag. Available:")
            for k in sorted(known):
                print("   ", k)
            sys.exit(2)
        print(f"\nfetching {n} ...", flush=True)
        m = timm.create_model(n, pretrained=True, num_classes=0)
        p = sum(q.numel() for q in m.parameters())
        print(f"  ok: {p/1e6:,.0f} M parameters, embed_dim {m.embed_dim}, "
              f"{len(m.blocks)} blocks, patch {m.patch_embed.patch_size[0]}")
        print(f"  bf16 weights would occupy {p*2/1e9:.1f} GB on the card")
        del m
        torch.cuda.empty_cache() if torch.cuda.is_available() else None
    print("\nall weights cached. slurm_train.sh can now run with HF_HUB_OFFLINE=1.")


if __name__ == "__main__":
    main()
