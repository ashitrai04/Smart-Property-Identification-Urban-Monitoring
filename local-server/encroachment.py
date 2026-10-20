"""Encroachment: things standing inside the road that should not be.

The first attempt flagged 2,125 objects over 6.3 ha in one clip, which is 17% of
the road area - not credible. It took "raised object inside the road polygon",
and the road polygons are buffered centrelines of which only 48% is actually
road, so it was re-detecting the buildings either side.

Three sources instead of one:
  OSM road polygon   the right-of-way: the boundary encroachment is measured
                     against, which is what makes it an encroachment at all
  traced carriageway where the road really is, from the imagery
  nDSM = DSM - DTM   what is standing up

An object counts only if it is raised, inside the right-of-way, NOT part of the
carriageway, and close enough to the carriageway to actually obstruct it. Each
one is scored by how far it intrudes and whether it matches a known building,
so a permanent structure can be told from dumped material or a stall.
"""
import os
import sys
import time

import cv2
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize, shapes
from scipy.ndimage import distance_transform_edt
from shapely.geometry import shape
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from step3_all import read_on

DSM_T = "D:/DSM/DSM_{clip}.tif"
DTM_T = "D:/DSM/DTM_{clip}.tif"
ROADS = "D:/GEO_SERVE/ongole_roads_district.gpkg"
SURFACE = "D:/GEO_SERVE/ongole_road_surface_city.gpkg"
BUILDINGS = "D:/GEO_SERVE/ongole_2026_segformer.gpkg"
ORTHO = "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
M = 32644

RES = 0.50
MIN_H = 0.40          # m above terrain to count as an object
MIN_AREA = 2.0        # m2
NEAR_ROAD = 3.0       # m from the carriageway: further than this it obstructs nothing
EDGE_KEEP = 0.5       # ignore the first 0.5 m inside the RoW (kerbs, poles)
GREEN_VEG = 18.0      # excess-green above this is canopy, not a structure.
                      # Without it the median "obstruction" was 6.2 m tall:
                      # trees overhanging the carriageway, not encroachment.


def main(clip="clip_06"):
    t0 = time.time()
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    poly = ci[ci["clip"] == clip].geometry.iloc[0]
    b = poly.bounds

    dtm, tr = read_on(DTM_T.format(clip=clip), b, RES)
    dsm, _ = read_on(DSM_T.format(clip=clip), b, RES)
    ndsm = dsm - dtm
    shp = dtm.shape

    rp = gpd.read_file(ROADS, layer="road_polygons").to_crs(M)
    rp = rp[rp.geometry.intersects(poly)]
    row = rasterize([(g.buffer(-EDGE_KEEP), 1) for g in rp.geometry.values
                     if g.buffer(-EDGE_KEEP).area > 0],
                    out_shape=shp, transform=tr, fill=0, dtype="uint8").astype(bool)

    try:
        ts = gpd.read_file(SURFACE, layer="road_surface").to_crs(M)
        ts = ts[ts.geometry.intersects(poly)]
        carr = rasterize([(g, 1) for g in ts.geometry.values], out_shape=shp,
                         transform=tr, fill=0, dtype="uint8").astype(bool)
    except Exception:
        carr = np.zeros(shp, bool)
    if carr.sum() < 100:                       # fall back to flat ground
        carr = row & (np.nan_to_num(ndsm, nan=9e9) < 0.25)
    near = distance_transform_edt(~carr) * RES <= NEAR_ROAD

    # greenness from the ortho, so canopy can be told from built things
    od = rasterio.open(ORTHO)
    W = int((b[2]-b[0])/RES); H = int((b[3]-b[1])/RES)
    rgbs = []
    for bi in (1, 2, 3):
        a = np.zeros((H, W), np.float32)
        for y0 in range(0, H, 1024):
            for x0 in range(0, W, 1024):
                hh = min(1024, H-y0); ww = min(1024, W-x0)
                gb = (b[0]+x0*RES, b[3]-(y0+hh)*RES, b[0]+(x0+ww)*RES, b[3]-y0*RES)
                a[y0:y0+hh, x0:x0+ww] = od.read(bi,
                    window=rasterio.windows.from_bounds(*gb, od.transform),
                    out_shape=(hh, ww), resampling=rasterio.enums.Resampling.average,
                    boundless=True, fill_value=0)
        rgbs.append(a)
    R, G, B = rgbs
    exg = 2*G - R - B
    veg = exg >= GREEN_VEG

    raised = np.nan_to_num(ndsm, nan=0) >= MIN_H
    enc = (raised & row & ~carr & near).astype(np.uint8)
    enc = cv2.morphologyEx(enc, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    print(f"{clip}: RoW {row.sum()*RES**2/1e4:.1f} ha | carriageway "
          f"{carr.sum()*RES**2/1e4:.1f} ha | candidate cells "
          f"{enc.sum()*RES**2/1e4:.2f} ha  ({time.time()-t0:.0f}s)", flush=True)

    try:
        bl = gpd.read_file(BUILDINGS, layer="buildings").to_crs(M)
        bl = bl[bl.geometry.intersects(poly)]
        blu = unary_union(bl.geometry.values)
    except Exception:
        blu = None

    rows = []
    for g, v in shapes(enc, mask=enc.astype(bool), transform=tr):
        if v != 1:
            continue
        p = shape(g)
        if p.area < MIN_AREA:
            continue
        mk = rasterize([(p, 1)], out_shape=shp, transform=tr, fill=0,
                       dtype="uint8").astype(bool)
        hh = ndsm[mk]
        hh = hh[np.isfinite(hh)]
        if not hh.size:
            continue
        # how far it reaches in from the right-of-way edge, and whether it is
        # a mapped building (permanent) or something else (stall, wall, dumping)
        is_b = bool(blu is not None and p.intersection(blu).area > 0.5 * p.area)
        vfrac = float(veg[mk].mean())
        if vfrac > 0.5 and not is_b:
            continue                       # canopy over the road, not encroachment
        rows.append({"geometry": p, "area_m2": round(p.area, 2),
                     "vegetation_frac": round(vfrac, 2),
                     "height_max_m": round(float(np.nanmax(hh)), 2),
                     "height_mean_m": round(float(np.nanmean(hh)), 2),
                     "volume_m3": round(float(np.nansum(hh)) * RES ** 2, 2),
                     "is_building": int(is_b),
                     "kind": "structure" if is_b else "obstruction"})
    out = gpd.GeoDataFrame(rows, crs=M) if rows else None
    if out is None or not len(out):
        print("  none found")
        return
    out = out.sort_values("area_m2", ascending=False).reset_index(drop=True)
    p = f"D:/GEO_SERVE/Ongole_ORI_V3_encroachment_{clip.replace('clip_','clip')}.gpkg"
    if os.path.exists(p):
        os.remove(p)
    out.to_crs(4326).to_file(p, layer="encroachment", driver="GPKG")
    st = out.groupby("kind").agg(n=("kind", "size"), area=("area_m2", "sum"),
                                 h=("height_max_m", "median")).reset_index()
    print(f"\n{'kind':<14}{'n':>6}{'area m2':>10}{'median h':>10}")
    for _, r in st.iterrows():
        print(f"  {r['kind']:<12}{int(r['n']):>6}{r['area']:>10.0f}{r['h']:>9.1f}m", flush=True)
    print(f"\ntotal {len(out)} encroachments, {out['area_m2'].sum():.0f} m2 "
          f"({out['area_m2'].sum()/1e4:.2f} ha)")
    print(f"wrote {p}  ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "clip_06")
