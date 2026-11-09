"""Decoders for a frozen DINOv3 backbone, and why the sharp one is shaped as it is.

The blobbiness problem, stated precisely
---------------------------------------
DINOv3 is a plain ViT with patch 16. On 20 cm imagery one token covers 3.20 m
of ground, so a 3.5 m traffic lane is **1.09 tokens wide** and a kerb line is
0.047 tokens. The transformer cannot represent a carriageway edge. Whatever
the decoder does, every sharp edge in the output has to come from somewhere
other than the token grid.

UPerDecoder (the first attempt, kept for comparison) does not provide that
somewhere. It builds a "pyramid" by up- and down-sampling the *same* stride-16
maps, which adds no high-frequency information - only interpolated copies of
information that was never there - fuses at stride 8 (1.60 m), and then makes
one 8x bilinear jump to full resolution. An 8x bilinear upsample is a blur
kernel. That is the mechanism behind the soft, rounded output.

SharpDecoder
------------
Two changes, both aimed at the same thing.

1. **A convolutional stem on the raw image**, producing genuine features at
   strides 1, 2, 4 and 8 - 0.20, 0.40, 0.80 and 1.60 m. These carry the real
   high-frequency content: the kerb, the gutter, the shadow line. This is the
   idea behind ViT-Adapter, which the DINOv3 authors pair with Mask2Former
   precisely because "a plain ViT has no multi-scale output". The spatial
   prior is reproduced here with plain convolutions rather than deformable
   attention, deliberately: deformable attention needs a compiled CUDA op, and
   a job that dies compiling an extension on a cluster costs a queue slot.

2. **Progressive fusion, 16 -> 8 -> 4 -> 2 -> 1.** Each step upsamples by two,
   concatenates the stem feature at that stride, and convolves. The semantic
   signal is carried down to full resolution along a path that always has real
   detail beside it, instead of being smeared across eight pixels in one go.

An auxiliary edge head is supervised on the label's own boundary. Dice barely
moves when an edge is 5-10 px out, so without an explicit boundary signal
nothing in the loss pushes the edge to the right place.

What is deliberately not here
-----------------------------
Full ViT-Adapter plus Mask2Former is the recipe the DINOv3 paper uses and it
would very likely score higher. It needs mmcv/mmdet and a compiled deformable
attention kernel. If this decoder is not sharp enough, that is the next step
and it is worth the setup pain - but it should be attempted after a baseline
is safely trained, not before.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F


def gn(c):
    """GroupNorm everywhere: a 7B backbone forces small batches, where
    BatchNorm's statistics are noisy, and the pooling branch reduces to 1x1
    where BatchNorm cannot compute a statistic at all."""
    return nn.GroupNorm(min(32, c), c)


def cbr(i, o, k=3):
    return nn.Sequential(nn.Conv2d(i, o, k, padding=k // 2), gn(o), nn.ReLU(inplace=True))


# ───────────────────────────── the old one ─────────────────────────────
class UPerDecoder(nn.Module):
    """Pyramid pooling + FPN over four stride-16 taps, fused at stride 8."""

    def __init__(self, dim, ch=256, pools=(1, 2, 3, 6)):
        super().__init__()
        self.up = nn.Sequential(nn.ConvTranspose2d(dim, dim // 2, 2, 2),
                                gn(dim // 2), nn.ReLU(inplace=True))
        self.ppm = nn.ModuleList([
            nn.Sequential(nn.AdaptiveAvgPool2d(p), nn.Conv2d(dim, ch, 1),
                          gn(ch), nn.ReLU(inplace=True)) for p in pools])
        self.bottleneck = cbr(dim + len(pools) * ch, ch)
        self.lat = nn.ModuleList([cbr(dim // 2, ch, 1), cbr(dim, ch, 1), cbr(dim, ch, 1)])
        self.fpn = nn.ModuleList([cbr(ch, ch) for _ in range(3)])
        self.fuse = cbr(4 * ch, ch)
        self.out_ch = ch

    def forward(self, feats, image=None):
        f = [self.up(feats[0]), feats[1], feats[2], F.max_pool2d(feats[3], 2)]
        deep = f[-1]
        ppm = [deep] + [F.interpolate(p(deep), size=deep.shape[-2:], mode="bilinear",
                                      align_corners=False) for p in self.ppm]
        lat = [l(z) for l, z in zip(self.lat, f[:-1])]
        lat.append(self.bottleneck(torch.cat(ppm, 1)))
        for i in range(len(lat) - 1, 0, -1):
            lat[i - 1] = lat[i - 1] + F.interpolate(lat[i], size=lat[i - 1].shape[-2:],
                                                    mode="bilinear", align_corners=False)
        outs = [self.fpn[i](lat[i]) for i in range(3)] + [lat[-1]]
        top = outs[0].shape[-2:]
        outs = [outs[0]] + [F.interpolate(o, size=top, mode="bilinear", align_corners=False)
                            for o in outs[1:]]
        return self.fuse(torch.cat(outs, 1))


# ───────────────────────────── the sharp one ─────────────────────────────
class ConvStem(nn.Module):
    """Real multi-scale detail from the image, which the token grid lacks."""

    def __init__(self, widths=(32, 64, 96, 128)):
        super().__init__()
        w1, w2, w4, w8 = widths
        self.s1 = nn.Sequential(cbr(3, w1), cbr(w1, w1))                 # stride 1
        self.s2 = nn.Sequential(nn.Conv2d(w1, w2, 3, 2, 1), gn(w2),
                                nn.ReLU(inplace=True), cbr(w2, w2))      # stride 2
        self.s4 = nn.Sequential(nn.Conv2d(w2, w4, 3, 2, 1), gn(w4),
                                nn.ReLU(inplace=True), cbr(w4, w4))      # stride 4
        self.s8 = nn.Sequential(nn.Conv2d(w4, w8, 3, 2, 1), gn(w8),
                                nn.ReLU(inplace=True), cbr(w8, w8))      # stride 8
        self.widths = widths

    def forward(self, x):
        a = self.s1(x)
        b = self.s2(a)
        c = self.s4(b)
        d = self.s8(c)
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
        x = self.merge(x)                                   # stride 16

        s1, s2, s4, s8 = self.stem(image)

        def up(z, skip, block):
            z = F.interpolate(z, size=skip.shape[-2:], mode="bilinear", align_corners=False)
            return block(torch.cat([z, skip], 1))

        x = up(x, s8, self.up8)                             # stride 8
        x = up(x, s4, self.up4)                             # stride 4
        x = up(x, s2, self.up2)                             # stride 2
        x = up(x, s1, self.up1)                             # stride 1
        return x


DECODERS = {"uper": UPerDecoder, "sharp": SharpDecoder}
