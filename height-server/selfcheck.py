"""Verify the elevation data before the server is trusted with it.

A height server that starts cleanly and then returns None for every building
is worse than one that refuses to start, because the failure reaches the other
machine as missing data rather than as an error. So the checks that can only
be done against the real rasters are done here, once, at startup:

  - the folders exist and hold readable rasters
  - every clip is georeferenced, and in a projected CRS where metres are metres
  - the DSM and DTM actually overlap, and agree on elevation
  - a real polygon over a real clip produces a plausible height

Run:  python selfcheck.py            (start.bat runs it for you)
"""
import sys
import traceback
from pathlib import Path

# So `python selfcheck.py` works from any directory, and under `python -I`,
# which drops the script's own folder from the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

FAIL = []
WARN = []


def ok(msg):
    print(f"  [ok]   {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")
    FAIL.append(msg)


def warn(msg):
    print(f"  [warn] {msg}")
    WARN.append(msg)


def main():
    print("\n=== Building Height Server self-check ===\n")

    print("dependencies")
    try:
        import numpy, rasterio, shapely, pyproj, scipy, skimage, fastapi  # noqa: F401
        ok(f"rasterio {rasterio.__version__}, shapely {shapely.__version__}, "
           f"numpy {numpy.__version__}")
    except Exception as e:
        bad(f"a dependency is missing: {e}")
        print("\n  Install them with:  .venv\\Scripts\\python -m pip install -r requirements.txt")
        return 1

    import numpy as np
    from shapely.geometry import box

    from app import config
    from app.tiles import INDEX

    print("\nfolders")
    for label, p in (("DSM", config.DSM_DIR), ("DTM", config.DTM_DIR)):
        if p.exists():
            ok(f"{label}: {p}")
        else:
            bad(f"{label} folder not found: {p}")
    if FAIL:
        print("\n  Pass the right paths:")
        print('      start.bat "C:\\path\\to\\DSM Data" "C:\\path\\to\\DTM Data"')
        print("  or set them:")
        print('      set HS_DSM_DIR=C:\\path\\to\\DSM Data')
        return 1

    print("\nindexing clips")
    s = INDEX.build(force=True)
    for e in s["errors"]:
        warn(e)
    n_dsm = s["dsm"].get("count", 0)
    n_dtm = s["dtm"].get("count", 0)
    if n_dsm:
        g = s["dsm"]["gsd_m"]
        ok(f"{n_dsm} DSM clips, {s['dsm']['megapixels']} MP, "
           f"GSD {g['min']}-{g['max']} m")
    else:
        bad("no readable DSM rasters - nothing can be measured")
    if n_dtm:
        g = s["dtm"]["gsd_m"]
        ok(f"{n_dtm} DTM clips, GSD {g['min']}-{g['max']} m")
    else:
        warn("no DTM clips: ground will be estimated from the DSM around each "
             "building, which is less accurate in dense low-rise")
    if FAIL:
        return 1

    print("\nprojections")
    import pyproj
    for kind, tiles in (("DSM", INDEX.dsm), ("DTM", INDEX.dtm)):
        for t in tiles[:50]:
            crs = pyproj.CRS.from_user_input(t["crs"])
            if crs.is_geographic:
                bad(f"{kind} {t['name']} is in degrees ({t['crs']}); heights and "
                    "areas need a projected CRS such as EPSG:32644")
                break
        else:
            if tiles:
                ok(f"{kind} clips are projected: {', '.join(sorted({x['crs'] for x in tiles})[:3])}")

    print("\ncoverage")
    cov = s["dtm_covers_pct_of_dsm"]
    if n_dtm and cov is not None:
        if cov >= 80:
            ok(f"DTM covers {cov}% of the DSM area")
        elif cov > 0:
            warn(f"DTM covers only {cov}% of the DSM area; buildings outside it "
                 "fall back to the ring estimate")
        else:
            bad("DSM and DTM do not overlap at all - check that both folders "
                "hold the same survey area, and that neither is mis-georeferenced")
    if s["bounds_wgs84"]:
        w, so, e, n = s["bounds_wgs84"]
        ok(f"DSM covers {w:.5f},{so:.5f} to {e:.5f},{n:.5f} (lon/lat)")

    print("\nelevation agreement")
    if n_dsm and n_dtm:
        try:
            import rasterio
            from app.heights import _clean, _read_aligned, _read_window, _to_crs
            t = INDEX.dsm[0]
            ds = INDEX.open(t["path"])
            # A small patch in the middle of the first clip.
            cx = (ds.bounds.left + ds.bounds.right) / 2
            cy = (ds.bounds.bottom + ds.bounds.top) / 2
            side = 60.0
            b = (cx - side, cy - side, cx + side, cy + side)
            dsm, tr = _read_window(ds, b)
            if dsm is None or not np.any(np.isfinite(dsm)):
                warn("centre of the first DSM clip is empty; cannot compare")
            else:
                wgs = _to_crs(box(*b), "EPSG:4326", src_crs=ds.crs)
                dts = INDEX.covering(wgs, "DTM")
                if not dts:
                    warn("no DTM clip over the centre of the first DSM clip")
                else:
                    dtm = _read_aligned(INDEX.open(dts[0]["path"]), dsm.shape, tr,
                                        ds.crs, b)
                    if dtm is None:
                        warn("DTM read returned nothing over that patch")
                    else:
                        d = dsm - dtm
                        d = d[np.isfinite(d)]
                        lo, med, hi = np.nanpercentile(d, [2, 50, 98])
                        print(f"         DSM-DTM over a 120 m patch: "
                              f"p2={lo:.2f} median={med:.2f} p98={hi:.2f} m")
                        # The low tail, not the median: over mostly-bare ground
                        # the median is 0 whichever way round the folders are,
                        # but a surface model can never sit below the terrain
                        # model, so a negative p2 is decisive.
                        if lo < -1.5:
                            bad(f"the DSM reads {lo:.1f} m BELOW the DTM at the "
                                "2nd percentile, which is impossible. The two "
                                "folders are almost certainly swapped - pass "
                                "them the other way round.")
                        elif hi < 1.0:
                            warn("no relief anywhere in the patch; either it is "
                                 "bare ground, or DSM and DTM are the same data")
                        else:
                            ok(f"DSM stands {hi:.1f} m above DTM at the 98th "
                               "percentile - consistent with buildings")
        except Exception as e:
            warn(f"agreement check could not run: {e}")

    print("\nend-to-end measurement")
    if n_dsm:
        try:
            from app.heights import measure_one, _to_crs
            t = INDEX.dsm[0]
            ds = INDEX.open(t["path"])
            cx = (ds.bounds.left + ds.bounds.right) / 2
            cy = (ds.bounds.bottom + ds.bounds.top) / 2
            # Measure a grid of 12 m squares and keep whatever looks built.
            hits = 0
            for dx in range(-150, 151, 50):
                for dy in range(-150, 151, 50):
                    sq = box(cx + dx - 6, cy + dy - 6, cx + dx + 6, cy + dy + 6)
                    r = measure_one(_to_crs(sq, "EPSG:4326", src_crs=ds.crs))
                    if r["height_m"] is not None and r["height_m"] >= config.MIN_HEIGHT:
                        hits += 1
                        if hits == 1:
                            print(f"         sample: height={r['height_m']} m, "
                                  f"{r['floor_label']}, ground={r['ground_elev_m']} m, "
                                  f"source={r['height_source']}, "
                                  f"px={r['n_pixels']}")
            if hits:
                ok(f"{hits} of 49 test squares returned a building-like height")
            else:
                warn("no test square came back above the minimum height. That is "
                     "normal over open ground, but if the clip is built-up, "
                     "check HS_MIN_HEIGHT and that the DSM is in metres")
        except Exception:
            warn("measurement test raised:\n" + traceback.format_exc())

    print("\n" + "=" * 42)
    if FAIL:
        print(f"FAILED - {len(FAIL)} problem(s) must be fixed:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print(f"PASSED{f' with {len(WARN)} warning(s)' if WARN else ''} - safe to serve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
