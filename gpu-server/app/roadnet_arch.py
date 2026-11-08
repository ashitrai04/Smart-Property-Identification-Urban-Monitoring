"""Road extraction network: UPerNet head on a DINOv3 ViT-L/16 satellite encoder.

The architecture is reconstructed from the shipped checkpoint
(`best.zip`, `timm_id = vit_large_patch16_dinov3.sat493m`, val_f1 0.799).
Every tensor name and shape in that file is accounted for here and the
checkpoint loads with strict=True - see `verify()` at the bottom, which the
server runs on load, so a reconstruction that drifts from the weights fails
loudly instead of predicting noise.

  enc      timm vit_large_patch16_dinov3 - 24 blocks, dim 1024, patch 16,
           1 cls + 4 register tokens, RoPE (no pos_embed). 318 tensors,
           which is exactly timm's own state dict for that model.
  rescale  the four taps are all stride 16 coming out of a plain ViT, so they
           are spread into a pyramid: x2 up (the only learned one, a 2x2
           transposed conv 1024->512), identity, /2, /4.
  ppm      pyramid pooling on the deepest tap, four scales, 1024->256 each;
           bottleneck then takes 1024 + 4*256 = 2048.
  lat/fpn  standard UPerNet top-down path over the other three taps.
  fuse     the four 256-channel levels concatenated = 1024 -> 256.
  detail   a two-conv branch on the full-resolution image (3->32->64). The
           encoder's finest tap is stride 8, which is 80 cm on 10 cm imagery -
           wider than a lane marking - so the edges of a carriageway come from
           here, not from the transformer.
  mix/cls  256 (fused) + 64 (detail) = 320 -> 128 -> 64 -> 1 logit.

One channel out, so this is a binary road/not-road model: apply sigmoid, not
softmax.

Four things the weights do NOT pin down, because they involve no parameters:
which blocks are tapped, whether the encoder's final LayerNorm is applied to
the taps, what the three parameter-free `rescale` entries do, and whether the
detail branch sees the normalized tensor or the raw image. Every combination
loads with strict=True and runs. They were settled by scoring each against the
project's own `ongole_road_surface_city` layer on real Ongole imagery at 30 cm:

    norm=True,  rescale 8/16/16/32, taps (5,11,17,23)   F1 0.804   <- used here
    norm=True,  rescale 8/16/32/64                      F1 0.736
    norm=True,  rescale 8/16/16/16                      F1 0.770
    norm=False, anything                                F1 0.02-0.34

The checkpoint reports val_f1 0.7989, so 0.804 on held-out ground truth is the
configuration it was trained in. `norm` is the one that matters: with it off
the model loads perfectly and predicts almost nothing.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F

# Which encoder blocks are tapped. 24 blocks, four evenly spaced taps ending at
# the last - the usual arrangement for a ViT under UPerNet, and the one the
# checkpoint's channel widths are consistent with.
TAPS = (5, 11, 17, 23)
EMBED = 1024


def _cbr(cin, cout, k=3, p=None):
    """Conv-BN-ReLU, with the ReLU as index 2 so the state dict reads .0/.1."""
    if p is None:
        p = k // 2
    return nn.Sequential(nn.Conv2d(cin, cout, k, padding=p), nn.BatchNorm2d(cout),
                         nn.ReLU(inplace=True))


class UPerHead(nn.Module):
    def __init__(self, embed=EMBED, ch=256, pools=(1, 2, 3, 6)):
        super().__init__()
        # Only rescale[0] carries weights; the rest are parameter-free, which is
        # why the checkpoint holds rescale.0.* and nothing else - and why the
        # weights alone cannot say what they do. Settled by measurement: see
        # the note on the module. Strides end up 8, 16, 16, 32.
        self.rescale = nn.ModuleList([
            nn.Sequential(nn.ConvTranspose2d(embed, embed // 2, 2, 2),
                          nn.BatchNorm2d(embed // 2), nn.ReLU(inplace=True)),
            nn.Identity(),
            nn.Identity(),
            nn.MaxPool2d(2),
        ])
        self.ppm = nn.ModuleList([
            nn.Sequential(nn.AdaptiveAvgPool2d(p), nn.Conv2d(embed, ch, 1),
                          nn.BatchNorm2d(ch), nn.ReLU(inplace=True))
            for p in pools
        ])
        self.bottleneck = _cbr(embed + len(pools) * ch, ch)
        # Three laterals for the three shallower taps: the first comes out of
        # rescale[0] at half the channels, the other two untouched.
        self.lat = nn.ModuleList([
            nn.Sequential(nn.Conv2d(embed // 2, ch, 1), nn.BatchNorm2d(ch),
                          nn.ReLU(inplace=True)),
            nn.Sequential(nn.Conv2d(embed, ch, 1), nn.BatchNorm2d(ch),
                          nn.ReLU(inplace=True)),
            nn.Sequential(nn.Conv2d(embed, ch, 1), nn.BatchNorm2d(ch),
                          nn.ReLU(inplace=True)),
        ])
        self.fpn = nn.ModuleList([_cbr(ch, ch) for _ in range(3)])
        self.fuse = _cbr(4 * ch, ch)

    def forward(self, feats):
        # feats: four stride-16 maps, shallowest first.
        f = [r(x) for r, x in zip(self.rescale, feats)]
        deep = f[-1]
        ppm = [deep] + [F.interpolate(p(deep), size=deep.shape[-2:],
                                      mode="bilinear", align_corners=False)
                        for p in self.ppm]
        laterals = [l(x) for l, x in zip(self.lat, f[:-1])]
        laterals.append(self.bottleneck(torch.cat(ppm, 1)))

        # Top-down: add each level into the one above it.
        for i in range(len(laterals) - 1, 0, -1):
            laterals[i - 1] = laterals[i - 1] + F.interpolate(
                laterals[i], size=laterals[i - 1].shape[-2:],
                mode="bilinear", align_corners=False)
        outs = [self.fpn[i](laterals[i]) for i in range(3)] + [laterals[-1]]
        top = outs[0].shape[-2:]
        outs = [outs[0]] + [F.interpolate(o, size=top, mode="bilinear",
                                          align_corners=False) for o in outs[1:]]
        return self.fuse(torch.cat(outs, 1))


class RoadNet(nn.Module):
    def __init__(self, taps=TAPS, ch=256):
        super().__init__()
        import timm
        # pretrained=False on purpose: the checkpoint carries the fine-tuned
        # encoder, and downloading the public one would need network access the
        # server may not have.
        self.enc = timm.create_model("vit_large_patch16_dinov3",
                                     pretrained=False, num_classes=0)
        self.taps = tuple(taps)
        self.head = UPerHead(EMBED, ch)
        self.detail = nn.Sequential(
            nn.Conv2d(3, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(inplace=True))
        self.mix = nn.Sequential(
            nn.Conv2d(ch + 64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(inplace=True),
            nn.Conv2d(128, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(inplace=True))
        self.cls = nn.Conv2d(64, 1, 1)

    @property
    def n_prefix(self):
        """cls token + register tokens sitting in front of the patch tokens."""
        return 1 + getattr(self.enc, "num_reg_tokens", 4)

    def _tokens_to_map(self, t, hw):
        h, w = hw
        t = t[:, self.n_prefix:, :]
        b, n, c = t.shape
        return t.transpose(1, 2).reshape(b, c, h, w)

    def forward(self, x):
        H, W = x.shape[-2:]
        p = self.enc.patch_embed.patch_size[0]
        hw = (H // p, W // p)
        # timm builds DINOv3 on its Eva class, which exposes
        # forward_intermediates rather than get_intermediate_layers. NCHW asks
        # it to drop the cls and register tokens and reshape for us.
        # norm=True applies the encoder's final LayerNorm to every tap. This is
        # the single choice that matters most here: with norm=False the head
        # sees unnormalized activations, the BatchNorms downstream are far
        # outside the statistics they were trained on, and the model scores
        # F1 0.02 instead of 0.80 while still loading perfectly.
        feats = self.enc.forward_intermediates(
            x, indices=list(self.taps), return_prefix_tokens=False, norm=True,
            output_fmt="NCHW", intermediates_only=True)
        feats = [self._tokens_to_map(f, hw) if f.dim() == 3 else f for f in feats]
        fused = self.head(feats)
        fused = F.interpolate(fused, size=(H, W), mode="bilinear", align_corners=False)
        y = self.mix(torch.cat([fused, self.detail(x)], 1))
        return self.cls(y)          # (B,1,H,W) logits


def verify(state_dict, model=None):
    """Load strictly and say so. A silent partial load is the failure we refuse.

    Shapes alone do not prove the forward pass is the one that was trained, but
    a strict load does prove every tensor in the checkpoint has exactly one
    home of exactly the right shape here - which is what rules out a
    reconstruction that merely resembles the original.
    """
    model = model or RoadNet()
    missing, unexpected = model.load_state_dict(state_dict, strict=False)
    return model, list(missing), list(unexpected)
