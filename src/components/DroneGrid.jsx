import { useCallback, useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import { Crosshair, Grid3x3 } from "lucide-react";
import { gpuBase, probeGpu, useGpuStatus } from "../lib/modelApi";
import { Pill, SectionHeader, ToggleRow } from "./ui";

/*
 * Ongole drone survey grid — 24 cells, one per drone clip (clip_01 = cell 1).
 * Drawn on top of everything (Sentinel's state-line treatment: lime glow + edge);
 * drone imagery and results from the GPU server are inserted beneath it, so the
 * boundary always stays readable. Cells whose drone clip is on the GPU server are
 * tinted teal.
 */
export const GRID_SRC = "ongole-grid";
export const GRID_BEFORE = "ongole-grid-fill";     // raster/vector overlays go below this id
const LIME = "#CCFF00";

export function clipNumber(img) {
    const m = /clip[\s_-]*0*(\d+)/i.exec(`${img?.name || ""} ${img?.captured || ""}`);
    return m ? Number(m[1]) : null;
}

export default function DroneGrid({ getMap }) {
    const gpu = useGpuStatus();
    const [on, setOn] = useState(false);
    const [grid, setGrid] = useState(null);
    const [withDrone, setWithDrone] = useState([]);   // clip numbers that have imagery on the GPU server
    const droneRef = useRef(withDrone);
    droneRef.current = withDrone;

    useEffect(() => {
        fetch("/data/ongole_drone_grid.geojson").then((r) => r.json()).then(setGrid).catch(() => setGrid(null));
    }, []);

    // Which clips have drone imagery on the GPU server (for the teal tint + popup).
    useEffect(() => {
        let live = true;
        (async () => {
            if (!(await probeGpu())) return;
            try {
                const im = await (await fetch(`${gpuBase()}/imagery`)).json();
                const ongole = im.filter((i) => i.status === "ready" && (i.district || "").toLowerCase() === "ongole");
                const nums = new Set(ongole.map(clipNumber).filter(Boolean));
                // A full mosaic has no clip number: mark every cell its footprint covers.
                const fc = grid || await (await fetch("/data/ongole_drone_grid.geojson")).json();
                for (const img of ongole.filter((i) => !clipNumber(i) && i.bounds)) {
                    const [w, s, e, n] = img.bounds;
                    for (const f of fc.features) {
                        const [cw, cs, ce, cn] = bboxOf({ features: [f] });
                        if (cw < e && ce > w && cs < n && cn > s) nums.add(f.properties.clip);
                    }
                }
                if (live) setWithDrone([...nums]);
            } catch { /* GPU server offline: grid still works */ }
        })();
        return () => { live = false; };
    }, [gpu.base, gpu.ok]);

    const paintTint = useCallback((map) => {
        if (!map.getLayer(GRID_BEFORE)) return;
        map.setPaintProperty(GRID_BEFORE, "fill-color",
            ["case", ["in", ["get", "clip"], ["literal", withDrone]], "#2DD4BF", LIME]);
        // The tint marks "drone data here" in the overview, then fades out as you zoom
        // in so it never washes over the drone imagery itself.
        const has = ["in", ["get", "clip"], ["literal", withDrone]];
        map.setPaintProperty(GRID_BEFORE, "fill-opacity",
            ["interpolate", ["linear"], ["zoom"], 11, ["case", has, 0.14, 0.03], 13.5, ["case", has, 0, 0.02]]);
    }, [withDrone]);

    useEffect(() => { const m = getMap(); if (m && on) paintTint(m); }, [paintTint, on, getMap]);

    const add = (map) => {
        if (map.getSource(GRID_SRC)) return;
        map.addSource(GRID_SRC, { type: "geojson", data: grid });
        map.addLayer({ id: GRID_BEFORE, type: "fill", source: GRID_SRC, paint: { "fill-color": LIME, "fill-opacity": 0.03 } });
        map.addLayer({ id: "ongole-grid-glow", type: "line", source: GRID_SRC,
            layout: { "line-join": "round", "line-cap": "round" },
            paint: { "line-color": LIME, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 6, 16, 14], "line-opacity": 0, "line-blur": 6,
                     "line-opacity-transition": { duration: 900 } } });
        map.addLayer({ id: "ongole-grid-line", type: "line", source: GRID_SRC,
            layout: { "line-join": "round" },
            paint: { "line-color": LIME, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.4, 16, 3], "line-opacity": 0,
                     "line-opacity-transition": { duration: 600 } } });
        // One label per cell, at its centre (a polygon label repeats once per map tile).
        map.addSource(`${GRID_SRC}-pts`, { type: "geojson", data: labelPoints(grid) });
        map.addLayer({ id: "ongole-grid-label", type: "symbol", source: `${GRID_SRC}-pts`, minzoom: 11,
            layout: { "text-field": ["get", "label"], "text-size": ["interpolate", ["linear"], ["zoom"], 11, 10, 15, 14],
                      "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"], "text-allow-overlap": false },
            paint: { "text-color": "#F7FEE7", "text-halo-color": "#0B1220", "text-halo-width": 1.6 } });
        requestAnimationFrame(() => {
            map.setPaintProperty("ongole-grid-glow", "line-opacity", 0.3);
            map.setPaintProperty("ongole-grid-line", "line-opacity", 1);
        });
        paintTint(map);
        map.on("click", GRID_BEFORE, onClick);
        map.on("mouseenter", GRID_BEFORE, onEnter);
        map.on("mouseleave", GRID_BEFORE, onLeave);
    };

    const remove = (map) => {
        map.off("click", GRID_BEFORE, onClick);
        map.off("mouseenter", GRID_BEFORE, onEnter);
        map.off("mouseleave", GRID_BEFORE, onLeave);
        for (const id of ["ongole-grid-label", "ongole-grid-line", "ongole-grid-glow", GRID_BEFORE]) if (map.getLayer(id)) map.removeLayer(id);
        if (map.getSource(GRID_SRC)) map.removeSource(GRID_SRC);
        if (map.getSource(`${GRID_SRC}-pts`)) map.removeSource(`${GRID_SRC}-pts`);
    };

    // Popup: which clip, and whether its drone imagery is on the server.
    // Stable handler (map.off needs the same function); reads the live clip list via a ref.
    const onClick = useCallback((e) => {
        const f = e.features?.[0];
        if (!f) return;
        const n = f.properties.clip;
        const has = droneRef.current.includes(n);
        new mapboxgl.Popup({ closeButton: true, maxWidth: "240px" })
            .setLngLat(e.lngLat)
            .setHTML(`<div class="popup-title">${f.properties.label} · Ongole</div>
                <div class="popup-row"><span class="popup-key">Area</span><span class="popup-value">${f.properties.area_km2} km²</span></div>
                <div class="popup-row"><span class="popup-key">Drone imagery</span><span class="popup-value" style="color:${has ? "#2DD4BF" : "#F5A524"}">${has ? "on server — toggle it under Server imagery" : "not uploaded yet"}</span></div>`)
            .addTo(e.target);
    }, []);

    const toggle = () => {
        const map = getMap();
        if (!map || !grid) return;
        if (on) { remove(map); setOn(false); return; }
        add(map);
        setOn(true);
        const b = bboxOf(grid);
        map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 50, duration: 1600 });
    };

    // A map rebuilt by a base-map switch loses its layers: re-add if the grid was on.
    useEffect(() => {
        const map = getMap();
        if (map && on && grid && !map.getSource(GRID_SRC)) add(map);
    });

    const fly = () => {
        const map = getMap();
        if (!map || !grid) return;
        const b = bboxOf(grid);
        map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 50, duration: 1400 });
    };

    return (
        <>
            <SectionHeader right={<Pill colour={withDrone.length ? "var(--signal)" : undefined} mono>{withDrone.length}/24 drone</Pill>}>
                Ongole drone survey
            </SectionHeader>
            <div className="flex items-center px-1.5 pb-1">
                <div className="min-w-0 flex-1">
                    <ToggleRow on={on} onClick={toggle} colour={LIME} label="Drone grid · 24 clips"
                        sub={grid ? "Ongole municipal area · click a cell for details" : "loading…"}
                        icon={<Grid3x3 size={12} style={{ color: LIME }} />} />
                </div>
                <button onClick={fly} title="Fly to Ongole" className="mr-1 rounded-[5px] p-1 hover:bg-[var(--surface-2)]" style={{ color: "var(--text-mute)" }}>
                    <Crosshair size={12} />
                </button>
            </div>
        </>
    );
}

function onEnter(e) { e.target.getCanvas().style.cursor = "pointer"; }
function onLeave(e) { e.target.getCanvas().style.cursor = ""; }

/* Area-weighted centroid of each cell's outer ring — inside the cell for these shapes. */
function labelPoints(fc) {
    return {
        type: "FeatureCollection",
        features: fc.features.map((f) => {
            const ring = f.geometry.type === "Polygon" ? f.geometry.coordinates[0] : f.geometry.coordinates[0][0];
            let a = 0, cx = 0, cy = 0;
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
                const k = ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
                a += k; cx += (ring[j][0] + ring[i][0]) * k; cy += (ring[j][1] + ring[i][1]) * k;
            }
            return { type: "Feature", properties: f.properties, geometry: { type: "Point", coordinates: [cx / (3 * a), cy / (3 * a)] } };
        }),
    };
}

function bboxOf(fc) {
    let w = 180, s = 90, e = -180, n = -90;
    for (const f of fc.features) {
        const rings = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
        for (const poly of rings) for (const [x, y] of poly[0]) { w = Math.min(w, x); s = Math.min(s, y); e = Math.max(e, x); n = Math.max(n, y); }
    }
    return [w, s, e, n];
}
