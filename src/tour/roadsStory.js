// "AI for Roads" story map — Guntur drone survey, driven by the guided walkthrough.
// Static data in /public/tour/roads (the drone image's road-damage detections scored
// against the Guntur ground-truth road shapefile); the drone imagery itself streams
// from the GPU server when it is online.
import mapboxgl from "mapbox-gl";
import { gpuBase, probeGpu } from "../lib/modelApi";
import { imageryBeforeId } from "../lib/mapOrder";

const BASE = "/tour/roads";
const GUNTUR_DRONE = "img_7055edabf8";
const S = { img: "story-drone", roads: "story-gt-roads", det: "story-det", heat: "story-heat" };
let story = null;
let markers = [];
let spin = null;

const load = async () => (story ||= await (await fetch(`${BASE}/story.json`)).json());
// Keep the subject clear of the left rail, the AOI card (top right) and the caption (bottom right).
const FRAME = { top: 70, left: 40, right: 460, bottom: 170 };
const fly = (map, opts) => new Promise((res) => {
    map.flyTo({ essential: true, curve: 1.35, padding: FRAME, ...opts });
    map.once("moveend", () => res());
    setTimeout(res, (opts.duration || 4000) + 800);
});
const stopSpin = () => { if (spin) { cancelAnimationFrame(spin); spin = null; } };

function centroids(fc) {
    return {
        type: "FeatureCollection",
        features: fc.features.map((f) => {
            const ring = f.geometry.coordinates[0];
            const x = ring.reduce((s, p) => s + p[0], 0) / ring.length, y = ring.reduce((s, p) => s + p[1], 0) / ring.length;
            return { type: "Feature", properties: f.properties, geometry: { type: "Point", coordinates: [x, y] } };
        }),
    };
}

function marker(map, n, lngLat) {
    const el = document.createElement("div");
    el.className = "story-marker";
    el.innerHTML = `<span class="story-marker-pulse"></span><span class="story-marker-dot">${n}</span>`;
    return new mapboxgl.Marker({ element: el }).setLngLat(lngLat).addTo(map);
}

// Basemap shop / hospital / transit pins clutter the story: hide them while it runs.
function setPoi(map, visible) {
    for (const l of map.getStyle()?.layers || []) {
        if (l.type === "symbol" && /poi|transit|airport/i.test(l.id)) map.setLayoutProperty(l.id, "visibility", visible ? "visible" : "none");
    }
}

export async function start(map) {
    const st = await load();
    stopSpin();
    for (const id of [`${S.heat}-glow`, S.heat, `${S.det}-label`, `${S.det}-line`, `${S.det}-fill`, `${S.roads}-line`, `${S.roads}-fill`, S.img])
        if (map.getLayer(id)) map.removeLayer(id);
    for (const id of [S.heat, S.det, S.roads, S.img]) if (map.getSource(id)) map.removeSource(id);

    // drone imagery right on the basemap
    if (await probeGpu()) {
        map.addSource(S.img, { type: "raster", tileSize: 256, maxzoom: 22, bounds: st.summary.bounds,
            tiles: [`${gpuBase()}/imagery/${GUNTUR_DRONE}/tiles/{z}/{x}/{y}.webp`] });
        map.addLayer({ id: S.img, type: "raster", source: S.img, paint: { "raster-opacity": 1 } }, imageryBeforeId(map));
    }
    const all = await (await fetch(`${BASE}/guntur_detections.geojson`)).json();
    // verified road defects only (on a ground-truth road, not on a roof, pothole-sized)
    const det = { ...all, features: all.features.filter((f) => f.properties.on_road === 1) };
    map.addSource(S.roads, { type: "geojson", data: `${BASE}/guntur_gt_roads.geojson` });
    map.addSource(S.det, { type: "geojson", data: det });
    map.addSource(S.heat, { type: "geojson", data: centroids(det) });

    map.addLayer({ id: `${S.roads}-fill`, type: "fill", source: S.roads,
        paint: { "fill-color": "#22D3EE", "fill-opacity": 0, "fill-opacity-transition": { duration: 1200 } } });
    map.addLayer({ id: `${S.roads}-line`, type: "line", source: S.roads,
        paint: { "line-color": "#67E8F9", "line-width": ["interpolate", ["linear"], ["zoom"], 14, 0.6, 19, 2], "line-blur": 0.4,
                 "line-opacity": 0, "line-opacity-transition": { duration: 1200 } } });
    // overview: one crisp glowing dot per detection; boxes + labels take over when zoomed in
    map.addLayer({ id: `${S.heat}-glow`, type: "circle", source: S.heat, maxzoom: 18.2,
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 5, 18, 11], "circle-color": "#EF4444", "circle-blur": 1,
                 "circle-opacity": 0.35, "circle-opacity-transition": { duration: 1400 } } });
    map.addLayer({ id: S.heat, type: "circle", source: S.heat, maxzoom: 18.2,
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 1.8, 18, 4], "circle-color": "#EF4444",
                 "circle-stroke-color": "#FFFFFF", "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 14, 0.3, 18, 1],
                 "circle-opacity": 1, "circle-stroke-opacity": 0.9,
                 "circle-opacity-transition": { duration: 1400 }, "circle-stroke-opacity-transition": { duration: 1400 } } });
    // raw detections: every box the model found, before any filter
    map.addLayer({ id: `${S.det}-fill`, type: "fill", source: S.det, minzoom: 17.5,
        paint: { "fill-color": ["case", ["==", ["get", "label"], "Pothole"], "#EF4444", "#F5A524"], "fill-opacity": 0.3,
                 "fill-opacity-transition": { duration: 1400 } } });
    map.addLayer({ id: `${S.det}-line`, type: "line", source: S.det, minzoom: 17.5,
        paint: { "line-color": ["case", ["==", ["get", "label"], "Pothole"], "#EF4444", "#F5A524"], "line-width": 2, "line-opacity": 1,
                 "line-opacity-transition": { duration: 1400 } } });
    map.addLayer({ id: `${S.det}-label`, type: "symbol", source: S.det, minzoom: 18,
        layout: { "text-field": ["concat", ["get", "label"], " ", ["number-format", ["get", "confidence"], { "min-fraction-digits": 2, "max-fraction-digits": 2 }]],
                  "text-size": 11.5, "text-offset": [0, -1.3], "text-allow-overlap": true, "text-ignore-placement": true,
                  "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"] },
        paint: { "text-color": "#FFFFFF", "text-halo-color": "#B91C1C", "text-halo-width": 1.6, "text-opacity-transition": { duration: 1400 } } });

    const [w, s, e, n] = st.summary.bounds;
    setPoi(map, false);
    await fly(map, { center: [(w + e) / 2, (s + n) / 2], zoom: 15.4, pitch: 0, bearing: 0, duration: 4500 });
}

// Show the ground-truth road network the detections were verified against.
export async function filter(map) {
    map.setPaintProperty(`${S.roads}-line`, "line-opacity", 0.85);
    await fly(map, { zoom: 15.9, pitch: 0, bearing: 0, duration: 2200 });
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// On-map badge used while the layers are switched off for validation.
function badge(map, text) {
    const host = map.getContainer();
    let el = host.querySelector(".story-badge");
    if (!text) { el?.classList.remove("on"); return; }
    if (!el) { el = document.createElement("div"); el.className = "story-badge"; host.appendChild(el); }
    el.textContent = text;
    requestAnimationFrame(() => el.classList.add("on"));
}

// Fade the detection + road overlays off (raw drone image only) or back on.
function overlays(map, on) {
    map.setPaintProperty(`${S.det}-fill`, "fill-opacity", on ? 0.3 : 0);
    map.setPaintProperty(`${S.det}-line`, "line-opacity", on ? 1 : 0);
    map.setPaintProperty(`${S.det}-label`, "text-opacity", on ? 1 : 0);
    map.setPaintProperty(`${S.roads}-line`, "line-opacity", on ? 0.85 : 0);
}

// Hotspot i: fly to the cluster, push in to its most confident pothole, then validate
// it on the raw drone image by switching the layers off and on again.
export async function stop(map, i) {
    const st = await load();
    const p = st.stops[i];
    stopSpin();
    markers.forEach((m) => m.remove());
    markers = [marker(map, i + 1, p.center)];
    overlays(map, true);
    await fly(map, { center: p.center, zoom: 19.6, pitch: 0, bearing: 0, duration: 2800 });
    await wait(700);
    markers.forEach((m) => m.remove());
    await fly(map, { center: p.best, zoom: 21.4, pitch: 0, bearing: 0, duration: 1600, curve: 1.1 });
    await wait(900);
    overlays(map, false);
    badge(map, "Layers off — raw drone image: the pothole is really there");
    await wait(2200);
    overlays(map, true);
    badge(map, `Layers on — Pothole · confidence ${p.best_conf.toFixed(2)} · ${p.best_size_m} m`);
    await wait(1700);
    badge(map, null);
}

export async function end(map) {
    stopSpin();
    badge(map, null);
    markers.forEach((m) => m.remove());
    markers = [];
    const st = await load();
    const [w, s, e, n] = st.summary.bounds;
    await fly(map, { center: [(w + e) / 2, (s + n) / 2], zoom: 15.4, pitch: 0, bearing: 0, duration: 3000 });
    map.setPadding({ top: 0, left: 0, right: 0, bottom: 0 });
    setPoi(map, true);
}
