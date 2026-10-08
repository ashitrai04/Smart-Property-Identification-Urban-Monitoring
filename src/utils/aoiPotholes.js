// Road damage (potholes / cracks) inside an AOI, from the GPU server's drone results.
// For every drone image whose footprint touches the AOI, take its newest road-damage
// result (ground-truth gated > refined > raw pothole job), keep the verified on-road
// detections, and count the ones whose centre falls inside each AOI polygon.
import { gpuBase, probeGpu } from "../lib/modelApi";

const PREFER = { gtgate: 3, refine: 2, pothole: 1 };

function ringContains(ring, x, y) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [xi, yi] = ring[i], [xj, yj] = ring[j];
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
}
function polyContains(geom, x, y) {
    const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.type === "MultiPolygon" ? geom.coordinates : [];
    return polys.some((rings) => ringContains(rings[0], x, y) && !rings.slice(1).some((h) => ringContains(h, x, y)));
}
function bboxOf(features) {
    let w = 180, s = 90, e = -180, n = -90;
    for (const f of features) {
        const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
        for (const p of polys) for (const [x, y] of p[0]) { w = Math.min(w, x); s = Math.min(s, y); e = Math.max(e, x); n = Math.max(n, y); }
    }
    return [w, s, e, n];
}
function centre(geom) {
    const ring = geom.type === "Polygon" ? geom.coordinates[0] : geom.coordinates[0][0];
    let x = 0, y = 0;
    for (const p of ring) { x += p[0]; y += p[1]; }
    return [x / ring.length, y / ring.length];
}

/** → { covered, total, potholes, cracks, meanConf, perPolygon: [{index, total, potholes}], sources: [names] }
 *    or null when the GPU server (which holds the drone results) is offline. */
export async function computeAOIPotholes(features, signal) {
    if (!features?.length || !(await probeGpu())) return null;
    const base = gpuBase();
    const [w, s, e, n] = bboxOf(features);
    const [imagery, jobs] = await Promise.all([
        fetch(`${base}/imagery`, { signal }).then((r) => r.json()),
        fetch(`${base}/jobs`, { signal }).then((r) => r.json()),
    ]);
    const touching = imagery.filter((i) => i.status === "ready" && i.kind !== "satellite" && i.bounds
        && i.bounds[0] < e && i.bounds[2] > w && i.bounds[1] < n && i.bounds[3] > s);
    const out = { covered: touching.length > 0, total: 0, potholes: 0, cracks: 0, meanConf: null, sources: [],
        perPolygon: features.map((_, index) => ({ index, total: 0, potholes: 0 })) };
    let confSum = 0;
    for (const img of touching) {
        const job = jobs.filter((j) => j.imagery_id === img.id && j.status === "done" && (j.layers || {}).road_damage && PREFER[j.task])
            .sort((a, b) => (PREFER[b.task] - PREFER[a.task]) || ((b.finished || 0) - (a.finished || 0)))[0];
        if (!job) continue;
        const bbox = [w, s, e, n].map((v) => v.toFixed(6)).join(",");
        const fc = await (await fetch(`${base}/results/${job.id}/road_damage.geojson?on_road=1&bbox=${bbox}&limit=50000`, { signal })).json();
        out.sources.push(img.name);
        for (const f of fc.features || []) {
            const [x, y] = centre(f.geometry);
            let hit = false;
            features.forEach((poly, k) => {
                if (polyContains(poly.geometry, x, y)) {
                    hit = true;
                    out.perPolygon[k].total += 1;
                    if (f.properties?.is_pothole) out.perPolygon[k].potholes += 1;
                }
            });
            if (!hit) continue;
            out.total += 1;
            if (f.properties?.is_pothole) out.potholes += 1; else out.cracks += 1;
            confSum += Number(f.properties?.confidence) || 0;
        }
    }
    out.meanConf = out.total ? +(confSum / out.total).toFixed(2) : null;
    return out;
}
