/**
 * Client-side exporters for analysis results.
 * The API returns a lossless grayscale PNG of raw class ids; from it we build:
 *   CSV      — class statistics (+ real m² when the input was geo-referenced)
 *   GeoJSON  — vectorized class polygons (d3-contour + RDP simplification)
 *   SHP      — zipped ESRI Shapefile (via @mapbox/shp-write)
 *   GeoTIFF  — georeferenced single-band class raster (via geotiff.js)
 */
import { contours as d3contours } from "d3-contour";
import { writeArrayBuffer } from "geotiff";
import * as shpwrite from "@mapbox/shp-write";

// ── download helper ──
export function downloadBlob(blob, filename) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// ── decode the grayscale class-id PNG into a Uint8Array grid ──
export function decodeClassMap(b64) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => {
            const c = document.createElement("canvas");
            c.width = img.width; c.height = img.height;
            const ctx = c.getContext("2d", { willReadFrequently: true });
            ctx.drawImage(img, 0, 0);
            const px = ctx.getImageData(0, 0, c.width, c.height).data;
            const out = new Uint8Array(c.width * c.height);
            for (let i = 0; i < out.length; i++) out[i] = px[i * 4]; // R channel = id
            resolve({ data: out, w: c.width, h: c.height });
        };
        img.onerror = () => reject(new Error("could not decode class map"));
        img.src = `data:image/png;base64,${b64}`;
    });
}

// ── approx metres-per-pixel from WGS84 bounds ──
function pixelAreaM2(bounds, w, h) {
    const R = 6371000, rad = (d) => (d * Math.PI) / 180;
    const midLat = (bounds.north + bounds.south) / 2;
    const wM = R * rad(bounds.east - bounds.west) * Math.cos(rad(midLat));
    const hM = R * rad(bounds.north - bounds.south);
    return (wM / w) * (hM / h);
}

// ── CSV of class statistics ──
export function exportCSV({ stats, classMap = null, bounds = null, baseName }) {
    const rows = [["Class", "Pixels", "Percent"]];
    let pxArea = null;
    if (bounds && classMap) {
        pxArea = pixelAreaM2(bounds, classMap.w, classMap.h);
        rows[0].push("Area_m2", "Area_km2");
    }
    Object.entries(stats || {}).forEach(([name, v]) => {
        const px = typeof v.pixels === "number" ? v.pixels : "";
        const r = [name, px, `${v.percent ?? ""}`];
        if (pxArea != null) {
            const m2 = typeof px === "number" ? px * pxArea : null;
            r.push(m2 != null ? Math.round(m2) : "", m2 != null ? (m2 / 1e6).toFixed(4) : "");
        }
        rows.push(r);
    });
    const csv = rows.map(r => r.map(x => `"${String(x).replace(/"/g, '""')}"`).join(",")).join("\n");
    downloadBlob(new Blob([csv], { type: "text/csv" }), `${baseName}_stats.csv`);
}

// ── Ramer–Douglas–Peucker ring simplification (pixel space) ──
function rdp(points, eps) {
    if (points.length < 4) return points;
    const sq = (v) => v * v;
    const dSeg = (p, a, b) => {
        const dx = b[0] - a[0], dy = b[1] - a[1];
        const l2 = sq(dx) + sq(dy);
        if (!l2) return Math.hypot(p[0] - a[0], p[1] - a[1]);
        let t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2;
        t = Math.max(0, Math.min(1, t));
        return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
    };
    const keep = new Uint8Array(points.length); keep[0] = keep[points.length - 1] = 1;
    const stack = [[0, points.length - 1]];
    while (stack.length) {
        const [s, e] = stack.pop();
        let maxD = 0, idx = -1;
        for (let i = s + 1; i < e; i++) {
            const d = dSeg(points[i], points[s], points[e]);
            if (d > maxD) { maxD = d; idx = i; }
        }
        if (maxD > eps && idx > 0) { keep[idx] = 1; stack.push([s, idx], [idx, e]); }
    }
    return points.filter((_, i) => keep[i]);
}

// ── vectorize class-id grid → GeoJSON FeatureCollection ──
export function classMapToGeoJSON({ data, w, h }, classDefs, bounds = null, colors = {}) {
    const present = new Set(data);
    const toGeo = bounds
        ? ([x, y]) => [
            +(bounds.west + (x / w) * (bounds.east - bounds.west)).toFixed(7),
            +(bounds.north - (y / h) * (bounds.north - bounds.south)).toFixed(7),
        ]
        : ([x, y]) => [Math.round(x * 100) / 100, -Math.round(y * 100) / 100]; // pixel coords, y flipped

    const features = [];
    const gen = d3contours().size([w, h]).smooth(false).thresholds([0.5]);
    const grid = new Float64Array(w * h);

    for (const idStr of Object.keys(classDefs)) {
        const id = Number(idStr);
        if (id === 0 || !present.has(id)) continue;
        for (let i = 0; i < grid.length; i++) grid[i] = data[i] === id ? 1 : 0;
        const [band] = gen(grid);
        if (!band || !band.coordinates.length) continue;
        const polys = band.coordinates
            .map(poly => poly
                .map(ring => {
                    // eps must stay < 0.5px: a 1-pixel-wide feature (thin road) deviates
                    // exactly 0.5px from its midline and vanishes at any higher tolerance
                    const simplified = rdp(ring, 0.4).map(toGeo);
                    // ensure closed ring
                    const f = simplified[0], l = simplified[simplified.length - 1];
                    if (f[0] !== l[0] || f[1] !== l[1]) simplified.push([...f]);
                    return simplified;
                })
                .filter(r => r.length >= 4))
            .filter(p => p.length);
        if (!polys.length) continue;
        features.push({
            type: "Feature",
            properties: { class_id: id, class_name: classDefs[idStr], color: colors[id] || null, crs: bounds ? "EPSG:4326" : "pixel" },
            geometry: { type: "MultiPolygon", coordinates: polys },
        });
    }
    return { type: "FeatureCollection", features };
}

export function exportGeoJSON(fc, baseName) {
    downloadBlob(new Blob([JSON.stringify(fc)], { type: "application/geo+json" }), `${baseName}.geojson`);
}

// ── zipped Shapefile (explode MultiPolygons for maximum tool compatibility) ──
export async function exportSHP(fc, baseName) {
    const exploded = { type: "FeatureCollection", features: [] };
    for (const f of fc.features) {
        if (f.geometry.type === "MultiPolygon") {
            for (const coords of f.geometry.coordinates) {
                exploded.features.push({ type: "Feature", properties: { ...f.properties }, geometry: { type: "Polygon", coordinates: coords } });
            }
        } else exploded.features.push(f);
    }
    const blob = await shpwrite.zip(exploded, { outputType: "blob", compression: "DEFLATE", types: { polygon: baseName } });
    downloadBlob(blob, `${baseName}_shp.zip`);
}

// ── georeferenced single-band GeoTIFF of class ids ──
export async function exportGeoTIFF({ data, w, h }, bounds, baseName) {
    const metadata = {
        width: w, height: h,
        SamplesPerPixel: 1, BitsPerSample: [8], PhotometricInterpretation: 1,
        ModelPixelScale: [(bounds.east - bounds.west) / w, (bounds.north - bounds.south) / h, 0],
        ModelTiepoint: [0, 0, 0, bounds.west, bounds.north, 0],
        GTModelTypeGeoKey: 2,      // geographic
        GTRasterTypeGeoKey: 1,     // pixel-is-area
        GeographicTypeGeoKey: 4326,
    };
    const ab = await writeArrayBuffer(Array.from(data), metadata);
    downloadBlob(new Blob([ab], { type: "image/tiff" }), `${baseName}_classes.tif`);
}
