// Ongole terrain story — pothole depth, waterlogging, encroachment.
//
// Same choreography as the Guntur pothole stops in roadsStory.js: fly to a
// cluster, push in to the single worst feature in it, switch the overlay off
// so the thing is visible on the raw 3 cm ortho, then back on with its
// numbers. A static fly-to shows a coloured blob and proves nothing; the
// off/on is what makes a claim checkable.
//
// Layers come off the live data server rather than /public, so the
// walkthrough always plays what is currently delivered, and hotspots are
// ranked from that data at run time — these layers get refiltered, and a
// hard-coded hotspot would quietly drift onto a feature that no longer exists.
import { gpuBase } from "../lib/modelApi";
import { imageryBeforeId } from "../lib/mapOrder";
import { _fx } from "./roadsStory";

const O = { src: "ong-layer", img: "ong-img" };
let cache = {};

const ONGOLE = {
    pothole_depth: {
        task: "pothole", label: "Pothole depth",
        metric: (p) => Number(p.depth_max_m) || 0,
        say: (p) => `Pothole · ${(Number(p.depth_max_m) * 100).toFixed(0)} cm deep · ${Number(p.volume_m3).toFixed(3)} m³ to fill`,
        colour: ["interpolate", ["linear"], ["coalesce", ["get", "depth_max_m"], 0],
                 0.02, "#FDE68A", 0.06, "#F59E0B", 0.12, "#DC2626"],
        zoomCluster: 19.2, zoomIn: 21.2,
    },
    waterlog: {
        task: "segment", label: "Waterlogging",
        metric: (p) => Number(p.volume_m3) || Number(p.max_depth_m) || 0,
        say: (p) => `Ponding · ${(Number(p.max_depth_m) * 100).toFixed(0)} cm deep · ${Number(p.volume_m3).toFixed(1)} m³`,
        colour: ["interpolate", ["linear"], ["coalesce", ["get", "max_depth_m"], 0],
                 0.03, "#93C5FD", 0.10, "#3B82F6", 0.25, "#1D4ED8"],
        zoomCluster: 18.4, zoomIn: 20.4,
    },
    encroachment: {
        task: "segment", label: "Encroachment",
        // Choosing the showcase took three passes, each fixing what the last
        // one surfaced:
        //   raw area        -> a 2,474 m², 15.3 m object: a stand of trees
        //   size + height   -> the central median of a dual carriageway
        //   + irregularity  -> a parked truck and a shadow on open highway
        // What a real encroachment has, and those do not, is a property it
        // belongs to: the good examples sit 0.5 m and 24 m from a building,
        // the truck 260 m and the shadow 141 m. So proximity to a building
        // carries the decision, with rectangular shapes rejected because a
        // vehicle is a neat rectangle and a stall or dumped load is not.
        metric: (p) => {
            const a = Number(p.area_m2) || 0;
            const h = Number(p.height_max_m) || 0;
            const fill = Number(p.rect_fill);
            const bld = Number(p.bld_m);
            if (a < 6 || a > 250 || h < 0.8 || h > 7) return 0;
            if (p.likely_vehicle === 1 || (fill >= 0.62)) return 0;
            if (Number.isFinite(bld) && bld > 40) return 0;   // out in open road
            return a * (p.kind === "structure" ? 1.6 : 1) * (1 - (Number.isFinite(fill) ? fill : 0.5));
        },
        say: (p) => `${p.kind === "structure" ? "Structure" : "Obstruction"} · ${Number(p.area_m2).toFixed(0)} m² · ${Number(p.height_max_m).toFixed(1)} m tall`,
        colour: ["case", ["==", ["get", "kind"], "structure"], "#DC2626", "#F97316"],
        zoomCluster: 18.2, zoomIn: 20.2,
    },
};

const centreOf = (g) => {
    if (!g) return null;
    if (g.type === "Point") return g.coordinates;
    const ring = g.type === "Polygon" ? g.coordinates[0] : g.coordinates?.[0]?.[0];
    if (!Array.isArray(ring) || !ring.length) return null;
    let x = 0, y = 0;
    for (const c of ring) { x += c[0]; y += c[1]; }
    return [x / ring.length, y / ring.length];
};

/* Rank hotspots: grid the features at ~250 m and score each cell by how many
 * it holds AND how bad its worst one is, so a cell with one deep hole can beat
 * a cell with ten shallow ones. Ranking on count alone picks dense clusters of
 * trivial defects, which is the least interesting thing to fly to. */
function hotspots(fc, metric, n = 3) {
    const CELL = 0.0025;
    const cells = new Map();
    for (const f of fc.features) {
        const c = centreOf(f.geometry);
        if (!c) continue;
        const k = `${Math.floor(c[0] / CELL)}:${Math.floor(c[1] / CELL)}`;
        const v = metric(f.properties || {});
        const cur = cells.get(k) || { n: 0, sx: 0, sy: 0, best: null, bestV: -1, bestProps: {} };
        cur.n++; cur.sx += c[0]; cur.sy += c[1];
        if (v > cur.bestV) { cur.bestV = v; cur.best = c; cur.bestProps = f.properties || {}; }
        cells.set(k, cur);
    }
    return [...cells.values()]
        .filter((c) => c.n >= 2 && c.best)
        .map((c) => ({ ...c, center: [c.sx / c.n, c.sy / c.n], score: c.n * Math.sqrt(1 + c.bestV) }))
        .sort((a, b) => b.score - a.score)
        .slice(0, n);
}

function overlay(map, on) {
    if (!map.getLayer(`${O.src}-fill`)) return;
    map.setPaintProperty(`${O.src}-fill`, "fill-opacity", on ? 0.5 : 0);
    map.setPaintProperty(`${O.src}-line`, "line-opacity", on ? 0.95 : 0);
}

function clear(map) {
    _fx.clearMarkers();
    for (const id of [`${O.src}-line`, `${O.src}-fill`, O.img]) if (map.getLayer(id)) map.removeLayer(id);
    for (const id of [O.src, O.img]) if (map.getSource(id)) map.removeSource(id);
}

async function jobFor(task) {
    const jobs = await (await fetch(`${gpuBase()}/jobs`)).json();
    const mine = jobs.filter((j) => j.task === task && String(j.id).includes("ongole"));
    // Three Ongole jobs share the "segment" task — 2017 satellite, 2026
    // satellite and the drone ortho — and taking the first match picked the
    // 2017 one, which carries no waterlogging or encroachment layer, so the
    // step silently did nothing. These layers belong to the drone ortho.
    const hit = mine.find((j) => String(j.id).includes("ongole-ori")) || mine[0];
    return hit ? hit.id : null;
}

/** Load a layer over the ortho and open on its worst area. */
export async function ongoleStart(map, which) {
    const spec = ONGOLE[which];
    if (!spec) return;
    _fx.stopSpin();
    clear(map);
    _fx.badge(map, null);

    const jid = await jobFor(spec.task);
    if (!jid) return;
    const fc = await (await fetch(`${gpuBase()}/results/${jid}/${which}.geojson`)).json();
    if (!fc.features?.length) return;
    cache[which] = { fc, spots: hotspots(fc, spec.metric) };

    const imgs = await (await fetch(`${gpuBase()}/imagery`)).json().catch(() => []);
    const ortho = (imgs || []).find((i) => String(i.id).includes("ongole-ori") && i.status === "ready");
    if (ortho) {
        map.addSource(O.img, { type: "raster", tileSize: 512, maxzoom: 21, bounds: ortho.bounds,
            tiles: [`${gpuBase()}/imagery/${ortho.id}/tiles/{z}/{x}/{y}.webp?size=512`] });
        map.addLayer({ id: O.img, type: "raster", source: O.img, paint: { "raster-opacity": 1 } }, imageryBeforeId(map));
    }
    map.addSource(O.src, { type: "geojson", data: fc });
    map.addLayer({ id: `${O.src}-fill`, type: "fill", source: O.src,
        paint: { "fill-color": spec.colour, "fill-opacity": 0, "fill-opacity-transition": { duration: 1100 } } });
    map.addLayer({ id: `${O.src}-line`, type: "line", source: O.src,
        paint: { "line-color": spec.colour, "line-width": ["interpolate", ["linear"], ["zoom"], 13, 0.9, 20, 2.6],
                 "line-opacity": 0, "line-opacity-transition": { duration: 1100 } } });

    const top = cache[which].spots[0];
    _fx.setPoi(map, false);
    await _fx.fly(map, { center: top ? top.center : centreOf(fc.features[0].geometry),
                         zoom: 15.2, pitch: 0, bearing: 0, duration: 3200 });
    overlay(map, true);
    _fx.badge(map, `${spec.label} — ${fc.features.length.toLocaleString()} across 24 clips`);
    await _fx.wait(1500);
    _fx.badge(map, null);
}

/** Hotspot i: fly in, push to the worst feature, prove it on the raw ortho. */
export async function ongoleStop(map, which, i) {
    const spec = ONGOLE[which];
    const c = cache[which];
    if (!spec || !c) return;
    const p = c.spots[i];
    if (!p) return;

    _fx.stopSpin();
    _fx.clearMarkers();
    _fx.pushMarker(_fx.marker(map, i + 1, p.center));
    overlay(map, true);
    await _fx.fly(map, { center: p.center, zoom: spec.zoomCluster, pitch: 0, bearing: 0, duration: 2600 });
    _fx.badge(map, `${p.n} within 250 m`);
    await _fx.wait(1000);

    _fx.clearMarkers();
    _fx.badge(map, null);
    await _fx.fly(map, { center: p.best, zoom: spec.zoomIn, pitch: 0, bearing: 0, duration: 1700, curve: 1.1 });
    await _fx.wait(800);

    overlay(map, false);
    _fx.badge(map, "Layers off — the raw drone image at 3 cm");
    await _fx.wait(2300);
    overlay(map, true);
    _fx.badge(map, spec.say(p.bestProps));
    await _fx.wait(1900);
    _fx.badge(map, null);
}

/** Pull back over the corridor so the layer reads as a pattern, not a pin. */
export async function ongoleWide(map) {
    _fx.clearMarkers();
    _fx.badge(map, null);
    await _fx.fly(map, { zoom: 13.6, pitch: 0, bearing: 0, duration: 3000 });
}

export async function ongoleEnd(map) {
    _fx.stopSpin();
    clear(map);
    _fx.badge(map, null);
    cache = {};
    map.setPadding({ top: 0, left: 0, right: 0, bottom: 0 });
    _fx.setPoi(map, true);
}
