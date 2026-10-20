#!/bin/bash
cd "D:/React folder/Smart-Property-Identification-Urban-Monitoring/local-server"
echo "=== waiting for earthen_fix ==="
until grep -qE "^updated .* tiles|Traceback" earthen.log 2>/dev/null; do sleep 30; done
tail -2 earthen.log
echo "=== verifying earthen gain on tracks ==="
python - <<'PY'
import numpy as np, rasterio, geopandas as gpd
from scipy.ndimage import map_coordinates
g = gpd.read_file("D:/GEO_SERVE/ongole_osm_roads_aligned_v2.gpkg", layer="osm_roads_aligned").to_crs(32644)
for path,tag in (("D:/GEO_SERVE/_kb/ongole_road_signal_backup.tif","before"),
                 ("D:/GEO_SERVE/_kb/ongole_road_signal.tif","after ")):
    ds=rasterio.open(path)
    A=ds.read(1,out_shape=(ds.height//2,ds.width//2),resampling=rasterio.enums.Resampling.average).astype(np.float32)/255.
    t=ds.transform; inv=~rasterio.Affine(t.a*2,0,t.c,0,t.e*2,t.f)
    def sc(sel):
        pts=[]
        for geom in g[sel].geometry.values:
            for ls in ([geom] if geom.geom_type=="LineString" else list(geom.geoms)):
                if ls.length<20: continue
                n=max(2,int(ls.length/5))
                pts+=[[p.x,p.y] for p in (ls.interpolate(i/(n-1),normalized=True) for i in range(n))]
        P=np.array(pts); c,r=inv*(P[:,0],P[:,1])
        v=map_coordinates(A,[r,c],order=1,mode="constant",cval=0)
        return 100*(v>0.35).mean()
    print(f"  {tag}: track {sc(g.fclass=='track'):.1f}%  unclassified {sc(g.fclass=='unclassified'):.1f}%  residential {sc(g.fclass=='residential'):.1f}%")
PY
echo "=== road_surface over the whole city ==="
python -u road_surface.py 2>&1 | grep -viE "warning|futurew|deprecat|np.reshape|read\("
