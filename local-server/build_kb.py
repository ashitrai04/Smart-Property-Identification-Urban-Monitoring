"""Build the Guntur road knowledge base the chatbot answers from.

The drone road layer has 1,046 segments with ids and widths but no names; the
damage layer has detections with no road. Neither answers "which road is worst"
on its own.

Two things this gets right that the obvious version does not:

  Names.  Segments are graph edges between junctions, averaging 60 m. Nobody
  asks about "segment R000652" — they ask about Arundelpet 6th line. Segments
  are matched to OSM street names and GROUPED, so a road is a road.

  Ranking.  Ranking by potholes/km puts a 9 m stub with one pothole (107/km) at
  the top of "most damaged road", which is noise, not a finding. Density is
  shrunk toward the mean by a 150 m prior and anything under MIN_RANK_M is
  excluded from ranking (still queryable by name).

Output
  D:\\GEO_SERVE\\guntur_roads_kb.gpkg   layers road_kb (per road), segment_kb
  D:\\GEO_SERVE\\_kb\\guntur_roads.json  the road table, for the ask API
"""
import json
import os
import time
import urllib.parse
import urllib.request

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge

ROADS = r"C:\Users\asash\Downloads\guntur_drone_roads\guntur_drone_roads.gpkg"
DAMAGE = r"D:\GEO_SERVE\guntur_road_damage.gpkg"
OUT_GPKG = r"D:\GEO_SERVE\guntur_roads_kb.gpkg"
OUT_DIR = r"D:\GEO_SERVE\_kb"
M = 32644
SNAP_M = 10.0       # detection -> segment
NAME_M = 25.0       # segment -> OSM street
PRIOR_KM = 0.30     # density shrinkage: 300 m of "average road" added to every road
MIN_RANK_M = 120.0  # shorter than this cannot be "the worst road"


def osm_names(bounds4326):
    """Named streets from OSM. Overpass wants form-encoded `data=`; a raw body
    gets 406."""
    w, s, e, n = bounds4326
    q = (f'[out:json][timeout:90];\n'
         f'(way["highway"]["name"]({s:.5f},{w:.5f},{n:.5f},{e:.5f}););\nout geom;')
    body = urllib.parse.urlencode({"data": q}).encode()
    for host in ("https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"):
        try:
            req = urllib.request.Request(host, data=body, headers={
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": "urbanly-road-kb/1.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=120).read())
            rows = []
            for el in d.get("elements", []):
                g = el.get("geometry") or []
                if len(g) >= 2:
                    rows.append({"name": el["tags"]["name"],
                                 "highway": el["tags"].get("highway", ""),
                                 "geometry": LineString([(p["lon"], p["lat"]) for p in g])})
            if rows:
                print(f"  OSM: {len(rows)} ways, {len({r['name'] for r in rows})} names", flush=True)
                return gpd.GeoDataFrame(rows, crs=4326).to_crs(M)
        except Exception as ex:
            print(f"  overpass {host.split('//')[1][:22]}: {str(ex)[:60]}", flush=True)
            time.sleep(2)
    return None


def _num(v, default=0.0):
    """NaN/None -> default. Guards the aggregate-of-empty-selection case."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if np.isnan(f) else f


def path_of(geoms):
    """One drivable path per road, for story mode. linemerge joins touching
    segments; the longest piece is the road's spine."""
    merged = linemerge(MultiLineString([g for g in geoms if g is not None and g.length > 0]))
    if isinstance(merged, LineString):
        return merged
    parts = sorted(merged.geoms, key=lambda s: s.length, reverse=True)
    return parts[0] if parts else None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    seg = gpd.read_file(ROADS, layer="road_centerlines").to_crs(M)
    seg = seg[seg.geometry.notna() & (seg.geometry.length > 1)].reset_index(drop=True)
    dmg = gpd.read_file(DAMAGE, layer="road_damage").to_crs(M)
    print(f"segments {len(seg)} | damage {len(dmg)}", flush=True)

    on = dmg[dmg["on_road"] == 1].reset_index(drop=True)
    pts = gpd.GeoDataFrame(on.drop(columns="geometry"), geometry=on.geometry.centroid, crs=M)
    j = gpd.sjoin_nearest(pts, seg[["seg_id", "geometry"]], how="left",
                          max_distance=SNAP_M, distance_col="snap_m")
    j = j[j["seg_id"].notna()]
    print(f"  {len(j)} of {len(pts)} on-road detections attached (<= {SNAP_M:.0f} m)", flush=True)

    agg = j.groupby("seg_id").agg(
        potholes=("is_pothole", "sum"), defects=("is_pothole", "size"),
        conf_max=("confidence", "max"), conf_mean=("confidence", "mean"),
        size_mean=("size_m", "mean"), size_max=("size_m", "max")).reset_index()
    seg = seg.merge(agg, on="seg_id", how="left")
    seg["potholes"] = seg["potholes"].fillna(0).astype(int)
    seg["defects"] = seg["defects"].fillna(0).astype(int)
    for c in ("conf_max", "conf_mean", "size_mean", "size_max"):
        seg[c] = seg[c].fillna(0.0)
    seg["length_m"] = seg.geometry.length

    # names
    nm = osm_names(tuple(seg.to_crs(4326).total_bounds))
    if nm is not None and len(nm):
        near = gpd.sjoin_nearest(
            gpd.GeoDataFrame(seg[["seg_id"]], geometry=seg.geometry.centroid, crs=M),
            nm[["name", "highway", "geometry"]], how="left",
            max_distance=NAME_M, distance_col="name_m")
        near = near.sort_values("name_m").drop_duplicates("seg_id")
        seg = seg.merge(near[["seg_id", "name", "highway"]], on="seg_id", how="left")
    else:
        print("  no OSM names available — roads fall back to segment ids", flush=True)
        seg["name"] = None
        seg["highway"] = None
    seg["name"] = seg["name"].fillna("")
    seg["highway"] = seg["highway"].fillna("")
    # unnamed segments stay addressable, each as its own road
    seg["road"] = np.where(seg["name"] != "", seg["name"], "Unnamed " + seg["seg_id"])
    named = int((seg["name"] != "").sum())
    print(f"  named {named} of {len(seg)} segments "
          f"({seg.loc[seg['name'] != '', 'name'].nunique()} distinct roads)", flush=True)

    rows = []
    for road, gdf in seg.groupby("road"):
        L = float(gdf["length_m"].sum())
        ph = int(gdf["potholes"].sum())
        df_ = int(gdf["defects"].sum())
        wt = gdf["length_m"] / max(L, 1e-9)
        p = path_of(list(gdf.geometry.values))
        rows.append({
            "road": road, "name": gdf["name"].iloc[0], "highway": gdf["highway"].iloc[0],
            "segments": len(gdf), "length_m": round(L, 1), "length_km": round(L / 1000, 3),
            "width_m": round(float((gdf["width_m"] * wt).sum()), 2),
            "area_m2": round(float((gdf["length_m"] * gdf["width_m"]).sum()), 1),
            "potholes": ph, "defects": df_,
            "conf_max": round(float(gdf["conf_max"].max()), 3),
            # NB: `np.nan or 0` is nan, not 0 — nan is truthy. Roads with no
            # defects must come out 0 here or their severity becomes NaN.
            "conf_mean": round(_num(gdf.loc[gdf["defects"] > 0, "conf_mean"].mean()), 3),
            "size_mean": round(_num(gdf.loc[gdf["defects"] > 0, "size_mean"].mean()), 2),
            "size_max": round(float(gdf["size_max"].max()), 2),
            "geometry": p if p is not None else gdf.geometry.iloc[0],
        })
    rd = gpd.GeoDataFrame(rows, crs=M)

    # shrunk density: a 40 m stub with one pothole no longer beats a 600 m road
    rd["per_km"] = (rd["potholes"] / (rd["length_km"] + PRIOR_KM)).round(2)
    rd["rankable"] = rd["length_m"] >= MIN_RANK_M
    d = rd["per_km"].clip(0, max(rd.loc[rd["rankable"], "per_km"].quantile(0.95), 1e-6))
    sev = (d / max(d.max(), 1e-9)) * 65 \
        + (rd["size_mean"] / max(rd["size_mean"].max(), 1e-9)) * 20 \
        + rd["conf_mean"] * 15
    rd["severity"] = sev.round(1)
    rd["grade"] = pd.cut(rd["severity"], [-0.1, 8, 20, 40, 65, 101],
                         labels=["good", "fair", "poor", "bad", "critical"]).astype(str)

    rk = rd[rd["rankable"]].sort_values("severity", ascending=False)
    rd["rank"] = 0
    rd.loc[rk.index, "rank"] = np.arange(1, len(rk) + 1)
    rd = rd.sort_values(["rankable", "severity"], ascending=[False, False]).reset_index(drop=True)

    rd4 = rd.to_crs(4326)
    rd4.to_file(OUT_GPKG, layer="road_kb", driver="GPKG")
    seg.to_crs(4326).to_file(OUT_GPKG, layer="segment_kb", driver="GPKG")

    tab = rd.drop(columns="geometry").copy()
    c = rd4.geometry.centroid
    tab["lon"] = c.x.round(6)
    tab["lat"] = c.y.round(6)
    b = rd4.bounds.round(6)
    tab["bbox"] = b[["minx", "miny", "maxx", "maxy"]].values.tolist()
    tab["rankable"] = tab["rankable"].astype(bool)
    with open(os.path.join(OUT_DIR, "guntur_roads.json"), "w", encoding="utf-8") as f:
        json.dump({"city": "Guntur", "built": time.strftime("%Y-%m-%d %H:%M"),
                   "source": {"roads": os.path.basename(ROADS), "damage": os.path.basename(DAMAGE)},
                   "totals": {"roads": int(len(rd)), "named": int((rd["name"] != "").sum()),
                              "length_km": round(float(rd["length_km"].sum()), 2),
                              "potholes": int(rd["potholes"].sum()),
                              "defects": int(rd["defects"].sum())},
                   "roads": json.loads(tab.to_json(orient="records"))}, f)

    print(f"\nwrote {OUT_GPKG}  (road_kb {len(rd)}, segment_kb {len(seg)})")
    print(f"      {OUT_DIR}\\guntur_roads.json")
    print(f"\nroads {len(rd)} | rankable {int(rd['rankable'].sum())} | "
          f"{rd['length_km'].sum():.1f} km | {int(rd['potholes'].sum())} potholes attached")
    print("\nworst 10 roads:")
    for _, r in rd[rd["rankable"]].head(10).iterrows():
        print(f"  {r['rank']:>3}. {r['road'][:34]:<34} sev {r['severity']:>5.1f} {r['grade']:<9} "
              f"{r['potholes']:>3} potholes {r['per_km']:>6.1f}/km  {r['length_m']:>6.0f} m")
    print("\ngrades:", rd["grade"].value_counts().to_dict())


if __name__ == "__main__":
    main()
