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

Sizes, so nobody is surprised by the download:
    vit_small_patch16_dinov3        21 M      ~0.1 GB
    vit_base_patch16_dinov3         86 M      ~0.3 GB
    vit_large_patch16_dinov3       300 M      ~1.2 GB
    vit_huge_plus_patch16_dinov3   840 M      ~3.4 GB
    vit_7b_patch16_dinov3         6716 M     ~13.4 GB
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
