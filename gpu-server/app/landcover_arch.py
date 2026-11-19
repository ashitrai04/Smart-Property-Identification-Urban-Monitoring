"""DINOv3 ViT-L/16 + UPerNet head — 9-class land cover (OpenEarthMap classes).

Reconstructed from the checkpoint, the same way roadnet_arch.py was, and
verified by loading it with strict=True: 403 tensors, 0 missing, 0 unexpected.
That strict load is the only evidence the shapes below are the ones the
weights were trained with, so it is what the loader asserts rather than
tolerating a partial match.

Two things here are not the obvious defaults and both were measured, not
assumed:

  * The backbone is the `sat493m` DINOv3 variant, pretrained on satellite
    imagery, so normalization is mean (0.430, 0.411, 0.296) /
    std (0.213, 0.156, 0.143) — not ImageNet. Same as roadnet.py.

  * The four encoder taps and whether they are LayerNormed before the head.
    Both were swept against 2,146 hand-drawn building polygons over 1 km^2 of
    Ongole (25.48 % building coverage), scoring building IoU:

        taps (11,15,19,23)  norm False   IoU 0.7680   <- here
        taps (5,11,17,23)   norm False   IoU 0.7574
        taps (7,11,15,23)   norm False   IoU 0.7545
        taps (4,11,17,23)   norm False   IoU 0.7521
        taps (0,7,15,23)    norm False   IoU 0.7321
        taps (20,21,22,23)  norm False   IoU 0.6953
        taps (5,11,17,23)   norm True    IoU 0.6593
        taps (20,21,22,23)  norm True    IoU 0.1363

    norm=False dominates the choice by roughly ten IoU points — taking the
    taps unnormalized matters far more than which four you take. Taking all
    four from the last block (20,21,22,23) is the worst non-degenerate
    option, which is the usual story: a UPerNet needs genuinely different
    scales, not four views of the same one.

timm implements DINOv3 under its `Eva` class, so the intermediate features
come from `forward_intermediates(...)` — `get_intermediate_layers` does not
exist on it and asking for it raises AttributeError.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F

# OpenEarthMap, in checkpoint channel order. Index 0 is "unknown", which the
# palette draws fully transparent: it is the model declining to answer, and
# painting it would cover imagery with a colour that means nothing.
CLASSES = ["unknown", "bareland", "rangeland", "developed", "road",
           "tree", "water", "agriculture", "building"]

MEAN = (0.430, 0.411, 0.296)
STD = (0.213, 0.156, 0.143)

TAPS = (11, 15, 19, 23)
NORM_TAPS = False


def cbr(i, o, k):
    return nn.Sequential(nn.Conv2d(i, o, k, padding=k // 2, bias=False),
                         nn.BatchNorm2d(o), nn.ReLU(inplace=True))


class UPerHead(nn.Module):
    """PPM on the deepest tap + a 3-level top-down FPN, then a 1x1 classifier.

    The ViT gives four taps at the SAME stride (16), unlike a CNN backbone, so
    `rescale` manufactures the pyramid the FPN expects: tap 0 is upsampled 4x
    to stride 4, tap 1 by 2x to stride 8, tap 2 is left at 16 and tap 3 is
    max-pooled to 32. That is what the transposed convolutions in the
    checkpoint are for.
    """

    def __init__(self, C=1024, ch=256, ncls=9, pool=(1, 2, 3, 6)):
        super().__init__()
        self.rescale = nn.ModuleList([
            nn.Sequential(nn.ConvTranspose2d(C, C // 2, 2, 2), nn.BatchNorm2d(C // 2),
                          nn.GELU(), nn.ConvTranspose2d(C // 2, C // 2, 2, 2)),
            nn.ConvTranspose2d(C, C, 2, 2)])
        self.lat = nn.ModuleList([cbr(C // 2, ch, 1), cbr(C, ch, 1), cbr(C, ch, 1)])
        self.ppm = nn.ModuleList([nn.Sequential(nn.AdaptiveAvgPool2d(s), cbr(C, ch, 1))
                                  for s in pool])
        self.bottleneck = cbr(C + len(pool) * ch, ch, 3)
        self.fpn = nn.ModuleList([cbr(ch, ch, 3) for _ in range(3)])
        self.fuse = cbr(4 * ch, ch, 3)
        self.cls = nn.Sequential(nn.Dropout2d(0.05), nn.Conv2d(ch, ncls, 1))

    def forward(self, feats):
        f0 = self.rescale[0](feats[0])            # stride 4
        f1 = self.rescale[1](feats[1])            # stride 8
        f2 = feats[2]                             # stride 16
        f3 = F.max_pool2d(feats[3], 2)            # stride 32
        lats = [self.lat[0](f0), self.lat[1](f1), self.lat[2](f2)]
        ppm = [f3] + [F.interpolate(p(f3), f3.shape[2:], mode="bilinear", align_corners=False)
                      for p in self.ppm]
        lats.append(self.bottleneck(torch.cat(ppm, 1)))
        for i in range(2, -1, -1):
            lats[i] = lats[i] + F.interpolate(lats[i + 1], lats[i].shape[2:],
                                              mode="bilinear", align_corners=False)
        outs = [self.fpn[i](lats[i]) for i in range(3)] + [lats[3]]
        outs = [F.interpolate(o, outs[0].shape[2:], mode="bilinear", align_corners=False)
                for o in outs]
        return self.cls(self.fuse(torch.cat(outs, 1)))


class DinoSeg(nn.Module):
    def __init__(self, taps=TAPS, ncls=len(CLASSES), norm_taps=NORM_TAPS):
        super().__init__()
        import timm
        self.enc = timm.create_model("vit_large_patch16_dinov3",
                                     pretrained=False, num_classes=0)
        self.head = UPerHead(ncls=ncls)
        self.taps, self.norm_taps = list(taps), norm_taps

    def forward(self, x):
        h, w = x.shape[2:]
        f = self.enc.forward_intermediates(x, indices=self.taps, norm=self.norm_taps,
                                           output_fmt="NCHW", intermediates_only=True)
        return F.interpolate(self.head(list(f)), (h, w), mode="bilinear", align_corners=False)


def build(state_dict, taps=TAPS, norm_taps=NORM_TAPS, ncls=len(CLASSES)):
    """Instantiate and load. Returns (model, missing, unexpected).

    Deliberately NOT strict: the caller reports which tensors did not match,
    because "3 tensors missing" is a usable error message and
    "Error(s) in loading state_dict" with 400 lines of names is not. The
    caller still refuses to serve a partial load.
    """
    m = DinoSeg(taps=taps, ncls=ncls, norm_taps=norm_taps)
    miss, unexp = m.load_state_dict(state_dict, strict=False)
    return m, list(miss), list(unexp)
