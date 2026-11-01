import { useCallback, useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import { Download, Play, Ruler, Square } from "lucide-react";
import { Button, Pill } from "./ui";
import { gpuOnlyFetch, useGpuStatus } from "../lib/modelApi";
import {
    floorBin, heightsAvailable, lastHeightProbe, measureFeatures, summarise,
} from "../lib/heights";
import { logActivity } from "../lib/activityLog";
import { takeFromMap } from "../lib/mapHandoff";

/*
 * Run a model over the polygon drawn on the map, and watch it work.
 *
 * The Analysis page answers "what is in this image I am uploading". This
 * answers "what is in this piece of ground", which is the question a survey
 * actually asks — and it needs no upload, because the orthomosaic is already
 * on the server.
 *
 * Nothing here is a fake progress animation. The server appends each window's
 * buildings to a live feed as that window finishes, and this polls it and
 * draws what has arrived, so a building appearing on the map means a building
 * was genuinely just found. Each one lands as the detector drew it and then
 * settles into its regularized outline a beat later — the two stages the
 * pipeline really has, paced so the change is visible rather than instant.
 */
const SRC = "area-model-src";
const FILL = "area-model-fill";
const LINE = "area-model-line";
const LABEL = "area-model-label";

/*
 * Height written on each roof.
 *
 * Only from this zoom: a 10 m building is about 30 px across at z16, which is
 * narrower than the text, so labelling below here produces a field of numbers
 * sitting on nothing legible. Mapbox drops labels that would collide rather
 * than drawing them on top of each other, so in dense fabric the ones that
 * survive are readable instead of all of them being a smear.
 */
const LABEL_MINZOOM = 16.5;

// How long a building shows its raw outline before the regularized one
// replaces it. Long enough to see the shape snap square, short enough that a
// busy window does not look stalled.
const SETTLE_MS = 700;

// A colour per building rather than per class: the point of this model is that
// these are separate buildings, and adjacent houses sharing a wall are only
// obviously separate when they are different colours. Hue is spread by the
// golden angle so neighbours drawn one after another never come out alike.
let hueCursor = Math.random() * 360;
function nextColour() {
    hueCursor = (hueCursor + 137.508) % 360;
    return `hsl(${hueCursor.toFixed(0)}, 70%, 58%)`;
}

/*
 * Storey count is a magnitude, not an identity, so it gets a single-hue
 * sequential ramp running light -> dark: taller reads darker, and the order is
 * legible without reading the legend. The multi-hue blue/green/amber/red map
 * the DSM suite shipped is the classic rainbow mistake — it encodes "more" as
 * a hue change, which carries no inherent order and breaks under colour
 * blindness.
 *
 * Six steps of the app's own signal hue (5 degrees of hue spread), checked for
 * monotone lightness, >= 0.06 adjacent lightness gaps, and contrast against
 * the dark surface. Adjacent steps in any sequential ramp are close by
 * construction, so colour alone never has to carry the exact value: the legend
 * is always drawn and clicking a building gives the number.
 */
/*
 * What gets written on the roof: the height, and the storey count under it.
 *
 * Two short lines rather than one long one, because a roof is roughly square
 * and a single "6.1 m · G+1" is wide enough to overflow a small house and be
 * dropped by collision detection — so the buildings that most need the label
 * would be the ones that lose it.
 */
function heightLabel(p) {
    if (p?.height_m == null) return undefined;
    const h = `${Number(p.height_m).toFixed(1)} m`;
    const f = p.floors == null ? null : (p.floors === 1 ? "G" : `G+${p.floors - 1}`);
    return f ? `${h}\n${f}` : h;
}

const FLOOR_COLOURS = ["#CCFBF1", "#7FE8DA", "#2DD4BF", "#14B8A6", "#0D9488", "#0B6B63"];
const UNMEASURED = "#64748B";          // --text-mute: present, but no height
const FLOOR_LABELS = ["1 (G)", "2 (G+1)", "3 (G+2)", "4 (G+3)", "5 (G+4)", "6+"];

const FLOOR_PAINT = [
    "match", ["get", "floorBin"],
    1, FLOOR_COLOURS[0], 2, FLOOR_COLOURS[1], 3, FLOOR_COLOURS[2],
    4, FLOOR_COLOURS[3], 5, FLOOR_COLOURS[4], 6, FLOOR_COLOURS[5],
    UNMEASURED,
];

function fmt(s) {
    if (s == null) return "—";
    if (s < 60) return `${Math.round(s)}s`;
    return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

const ring = (coords) => ({ type: "Polygon", coordinates: [coords] });

export default function AreaModelPanel({ map, aoiFeatures }) {
    const [tasks, setTasks] = useState([]);
    const [task, setTask] = useState("hisup");
    const [job, setJob] = useState(null);
    const [count, setCount] = useState(0);
    const [done, setDone] = useState(false);
    const [opacity, setOpacity] = useState(0.55);
    const [err, setErr] = useState("");
    const [handoff, setHandoff] = useState(null);
    const [heights, setHeights] = useState(null);     // running roll-up, or null
    const [heightNote, setHeightNote] = useState(""); // why there are no heights
    const [byFloors, setByFloors] = useState(false);  // colour by storeys, not identity
    const [labels, setLabels] = useState(true);       // height written on each roof
    const [saving, setSaving] = useState(false);

    const heightOkRef = useRef(false);
    const measuringRef = useRef(0);                   // batches still in flight

    const featsRef = useRef([]);          // every feature drawn so far
    const pollRef = useRef(null);
    const settleRef = useRef(null);
    const sinceRef = useRef(0);
    const gpu = useGpuStatus();

    const hasAoi = Array.isArray(aoiFeatures) && aoiFeatures.length > 0;
    const running = job && (job.status === "queued" || job.status === "running");

    useEffect(() => {
        let gone = false;
        gpuOnlyFetch("/area/tasks")
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (!gone && d?.tasks?.length) setTasks(d.tasks); })
            .catch(() => { /* offline; the panel already says so */ });
        return () => { gone = true; };
    }, [gpu.ok]);

    // ── the layer ───────────────────────────────────────────────
    const push = useCallback(() => {
        if (!map) return;
        const data = { type: "FeatureCollection", features: featsRef.current };
        const src = map.getSource(SRC);
        if (src) { src.setData(data); return; }
        map.addSource(SRC, { type: "geojson", data });
        map.addLayer({
            id: FILL, type: "fill", source: SRC,
            paint: {
                "fill-color": byFloors ? FLOOR_PAINT : ["get", "colour"],
                "fill-opacity": opacity,
            },
        });
        map.addLayer({
            id: LINE, type: "line", source: SRC,
            paint: {
                "line-color": byFloors ? FLOOR_PAINT : ["get", "colour"],
                // A building still showing the detector's outline is drawn
                // thicker, so the eye is drawn to what just happened.
                "line-width": ["case", ["==", ["get", "stage"], "raw"], 2.6, 1.2],
                "line-opacity": Math.min(1, opacity + 0.35),
            },
        });
        map.addLayer({
            id: LABEL, type: "symbol", source: SRC,
            minzoom: LABEL_MINZOOM,
            // Only buildings that actually have a height. `hlabel` is built in
            // JS rather than with a number-formatting expression, which Mapbox
            // has no clean way to do to one decimal place.
            filter: ["has", "hlabel"],
            layout: {
                "text-field": ["get", "hlabel"],
                "text-size": 11,
                "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
                "text-allow-overlap": false,
                "text-ignore-placement": false,
                // Centred on the roof, and never spilling outside it.
                "symbol-placement": "point",
                "text-max-width": 6,
                "visibility": labels ? "visible" : "none",
            },
            paint: {
                // White on a dark halo: the fills underneath run from pale to
                // deep teal, and imagery under that, so neither a light nor a
                // dark text colour alone stays readable across the ramp.
                "text-color": "#FFFFFF",
                "text-halo-color": "rgba(11,18,32,0.85)",
                "text-halo-width": 1.4,
            },
        });
    }, [map, opacity, byFloors, labels]);

    useEffect(() => {
        if (!map) return;
        try {
            map.setPaintProperty(FILL, "fill-opacity", opacity);
            map.setPaintProperty(LINE, "line-opacity", Math.min(1, opacity + 0.35));
        } catch { /* layer not added yet */ }
    }, [map, opacity]);

    // Switching between "a colour per building" and "colour by storeys" repaints
    // the existing layer rather than rebuilding it, so the map does not flicker.
    useEffect(() => {
        if (!map) return;
        const paint = byFloors ? FLOOR_PAINT : ["get", "colour"];
        try {
            map.setPaintProperty(FILL, "fill-color", paint);
            map.setPaintProperty(LINE, "line-color", paint);
        } catch { /* layer not added yet */ }
    }, [map, byFloors]);

    useEffect(() => {
        if (!map) return;
        try {
            map.setLayoutProperty(LABEL, "visibility", labels ? "visible" : "none");
        } catch { /* layer not added yet */ }
    }, [map, labels]);

    /*
     * Click a building for its exact numbers. A sequential ramp puts adjacent
     * storey classes close together by design, so this — not the colour — is
     * where a reader gets the value. `quality` is shown whenever it is not
     * "good", because a height nobody flagged and a height the server does not
     * trust must not look alike on the map.
     */
    useEffect(() => {
        if (!map || !heights?.measured) return;
        const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
        const row = (k, v) =>
            `<div class="popup-row"><span class="popup-key">${esc(k)}</span>` +
            `<span class="popup-value">${esc(v ?? "—")}</span></div>`;

        const onClick = (e) => {
            const p = e.features?.[0]?.properties;
            if (!p) return;
            const rows = [];
            if (p.height_m != null) {
                rows.push(row("Height", `${Number(p.height_m).toFixed(2)} m`));
                rows.push(row("Storeys", p.floor_label || p.floors));
                if (p.ground_elev_m != null) rows.push(row("Ground", `${p.ground_elev_m} m`));
                if (p.roof_elev_m != null) rows.push(row("Roof", `${p.roof_elev_m} m`));
                rows.push(row("Ground from", p.height_source === "dtm"
                    ? "surveyed DTM" : "estimated around it"));
            } else {
                rows.push(row("Height", "not measured"));
            }
            if (p.area_m2 != null) rows.push(row("Footprint", `${p.area_m2} m²`));
            if (p.quality && p.quality !== "good") {
                const why = {
                    no_dsm: "outside the DSM coverage",
                    low_pixels: "too few pixels to measure",
                    below_min_height: "below the minimum building height",
                    rough: "uneven roof — tree overhang, or two buildings merged",
                    no_ground: "no ground level could be established",
                }[p.quality] || p.quality;
                rows.push(row("Note", why));
            }
            new mapboxgl.Popup({ maxWidth: "260px" }).setLngLat(e.lngLat)
                .setHTML(`<div class="popup-title">Building</div>${rows.join("")}`)
                .addTo(map);
        };
        const enter = () => { map.getCanvas().style.cursor = "pointer"; };
        const leave = () => { map.getCanvas().style.cursor = ""; };
        map.on("click", FILL, onClick);
        map.on("mouseenter", FILL, enter);
        map.on("mouseleave", FILL, leave);
        return () => {
            map.off("click", FILL, onClick);
            map.off("mouseenter", FILL, enter);
            map.off("mouseleave", FILL, leave);
            try { leave(); } catch { /* map already gone */ }
        };
    }, [map, heights]);

    const clearLayer = useCallback(() => {
        if (!map) return;
        for (const id of [LABEL, FILL, LINE]) { try { map.getLayer(id) && map.removeLayer(id); } catch { /* gone */ } }
        try { map.getSource(SRC) && map.removeSource(SRC); } catch { /* gone */ }
        featsRef.current = [];
    }, [map]);

    // Swap settled buildings from the detector's outline to the regularized
    // one. Driven by a timer rather than by arrival so the change is visible
    // even when a whole window lands at once.
    useEffect(() => {
        settleRef.current = setInterval(() => {
            const now = Date.now();
            let changed = false;
            for (const f of featsRef.current) {
                if (f.properties.stage === "raw" && now - f.properties.t > SETTLE_MS) {
                    f.geometry = ring(f.properties._reg);
                    f.properties.stage = "reg";
                    changed = true;
                }
            }
            if (changed) push();
        }, 250);
        return () => clearInterval(settleRef.current);
    }, [push]);

    useEffect(() => () => {
        clearLayer();
        if (pollRef.current) clearInterval(pollRef.current);
    }, [clearLayer]);

    // ── a result handed over from Analysis ──────────────────────
    useEffect(() => {
        if (!map) return;
        const d = takeFromMap();
        if (!d) return;
        setHandoff(d);
        featsRef.current = (d.geojson.features || []).map((f, i) => ({
            type: "Feature",
            geometry: f.geometry,
            properties: {
                ...f.properties, colour: nextColour(), stage: "reg", t: 0, id: i,
                floorBin: floorBin(f.properties?.floors),
                hlabel: heightLabel(f.properties),
            },
        }));
        setCount(featsRef.current.length);
        setDone(true);
        push();
        // Analysis may already have measured these. If not — an older result,
        // or the laptop was off then — measure them now.
        if (featsRef.current.some((f) => f.properties.height_m != null)) {
            recount();
        } else {
            heightsAvailable().then((hp) => {
                heightOkRef.current = hp.ok;
                if (hp.ok) measureBatch(featsRef.current);
                else setHeightNote(hp.reason);
            });
        }
        try {
            const xs = [], ys = [];
            for (const f of featsRef.current) for (const [x, y] of f.geometry.coordinates[0]) { xs.push(x); ys.push(y); }
            if (xs.length) map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]],
                                         { padding: 60, duration: 800 });
        } catch { /* a bad ring should not stop the overlay showing */ }
        // takeFromMap consumes the handoff, so a re-run is a no-op early return.
    }, [map, push, measureBatch, recount]);

    /*
     * Heights, measured alongside detection rather than after it.
     *
     * Each window the detector finishes is handed straight to the DSM laptop
     * while the next window is still being detected, so the two machines work
     * at the same time and a building arrives already knowing how tall it is.
     * Batches are fired without being awaited: a slow or absent height server
     * must never hold up the detection feed.
     *
     * The regularized ring is measured, not the raw one — it is the outline
     * the pipeline settled on, and the raw one tends to include a strip of
     * wall and shadow that drags a roof down.
     */
    const recount = useCallback(() => {
        const s = summarise(featsRef.current.map((f) =>
            f.properties.quality || f.properties.height_m != null ? f.properties : null));
        setHeights(s.measured || Object.keys(s.quality).length ? s : null);
        if (s.measured) setByFloors(true);
    }, []);

    const measureBatch = useCallback(async (feats) => {
        if (!heightOkRef.current || !feats.length) return;
        measuringRef.current += 1;
        try {
            const props = await measureFeatures(
                feats.map((f) => ({ geometry: ring(f.properties._reg || f.geometry.coordinates[0]) })));
            if (!props) {
                // measureFeatures records why on the shared probe.
                const p = lastHeightProbe();
                if (!p.ok) { heightOkRef.current = false; setHeightNote(p.reason); }
                return;
            }
            props.forEach((p, i) => {
                if (!p) return;
                Object.assign(feats[i].properties, p);
                feats[i].properties.floorBin = floorBin(p.floors);
                const lbl = heightLabel(p);
                // `has` is the layer's filter, so an absent key means no label —
                // setting it to undefined would still count as present.
                if (lbl) feats[i].properties.hlabel = lbl;
            });
            push();
            recount();
        } catch { /* the run continues without heights for this batch */ } finally {
            measuringRef.current -= 1;
        }
    }, [push, recount]);

    // ── running ─────────────────────────────────────────────────
    const poll = useCallback((id) => {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = setInterval(async () => {
            try {
                const [liveRes, statRes] = await Promise.all([
                    gpuOnlyFetch(`/area/${id}/live?since=${sinceRef.current}`),
                    gpuOnlyFetch(`/area/${id}`),
                ]);
                if (statRes.ok) setJob(await statRes.json());
                if (!liveRes.ok) return;
                const d = await liveRes.json();
                sinceRef.current = d.next;

                if (d.items?.length) {
                    const added = [];
                    for (const it of d.items) {
                        const f = {
                            type: "Feature",
                            geometry: ring(it.raw),       // as detected; settles to `reg`
                            properties: {
                                colour: nextColour(), stage: "raw", t: Date.now(),
                                _reg: it.reg, area_m2: it.area_m2, floorBin: 0,
                            },
                        };
                        featsRef.current.push(f);
                        added.push(f);
                    }
                    setCount(featsRef.current.length);
                    push();
                    // Deliberately not awaited: the DSM laptop measures this
                    // window while the GPU detects the next one.
                    measureBatch(added);
                }

                if (["done", "failed", "cancelled"].includes(d.status)) {
                    clearInterval(pollRef.current); pollRef.current = null;
                    if (d.status === "done") {
                        setDone(true);
                        logActivity?.({ kind: "model", label: `Area run — ${featsRef.current.length} buildings` });
                    }
                }
            } catch { /* transient; the next tick retries */ }
        }, 1200);
    }, [push, measureBatch]);

    const start = async () => {
        if (!hasAoi) return;
        setErr(""); setDone(false); setJob(null); setCount(0); setHandoff(null);
        setHeights(null); setByFloors(false); setHeightNote("");
        clearLayer();

        // Asked once per run, not per window: if the DSM laptop is off, the run
        // should not spend a timeout on every batch discovering that again.
        const hp = await heightsAvailable(true);
        heightOkRef.current = hp.ok;
        if (!hp.ok) setHeightNote(hp.reason);

        sinceRef.current = 0;
        const aoi = aoiFeatures.length === 1
            ? aoiFeatures[0]
            : { type: "Feature", properties: {},
                geometry: { type: "MultiPolygon", coordinates: aoiFeatures.map((f) => f.geometry.coordinates) } };
        try {
            const r = await gpuOnlyFetch("/area", {
                method: "POST", headers: { "content-type": "application/json" },
                body: JSON.stringify({ task, aoi }),
            });
            const d = await r.json();
            if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
            setJob({ id: d.id, status: "queued", percent: 0 });
            poll(d.id);
        } catch (e) {
            setErr(e.message === "GPU_OFFLINE"
                ? "The GPU server is offline. Start it with start_everything.bat."
                : e.message);
        }
    };

    const cancel = async () => {
        if (!job) return;
        try { await gpuOnlyFetch(`/area/${job.id}/cancel`, { method: "POST" }); } catch { /* best effort */ }
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
        setJob((j) => (j ? { ...j, status: "cancelled" } : j));
        if (featsRef.current.length) setDone(true);   // partial results are still results
    };

    /*
     * Write the heights into the job's GeoPackage.
     *
     * Optional and explicit, because the server re-measures from the saved
     * file to do it — a second full pass over the DSM. The heights already on
     * screen go into the GeoJSON export for free; this is only for when the
     * .gpkg itself needs them, which is what the QGIS exports read.
     */
    const saveHeights = async () => {
        if (!job?.id) return;
        setSaving(true); setErr("");
        try {
            const r = await gpuOnlyFetch(`/height/area/${job.id}`, { method: "POST" });
            const d = await r.json();
            if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
            const st = d.height_status || {};
            if (st.saved_to) {
                setHeightNote("Saved into the result file.");
            } else {
                setErr(st.save_error || "The server measured but could not write the file.");
            }
        } catch (e) {
            setErr(e.message === "GPU_OFFLINE"
                ? "The GPU server is offline. Start it with start_everything.bat."
                : e.message);
        } finally {
            setSaving(false);
        }
    };

    const exportGeoJSON = () => {
        // Export what the pipeline settled on, not the in-flight outline.
        const out = {
            type: "FeatureCollection",
            features: featsRef.current.map((f, i) => {
                const p = f.properties;
                const out = { building_id: i + 1, area_m2: p.area_m2 ?? null };
                // Only once measured, so an export without heights carries no
                // empty columns implying the question was asked and answered.
                if (p.height_m != null || p.quality) {
                    Object.assign(out, {
                        height_m: p.height_m ?? null,
                        floors: p.floors ?? null,
                        floor_label: p.floor_label ?? null,
                        ground_elev_m: p.ground_elev_m ?? null,
                        roof_elev_m: p.roof_elev_m ?? null,
                        height_source: p.height_source ?? null,
                        quality: p.quality ?? null,
                    });
                }
                return {
                    type: "Feature",
                    geometry: p._reg ? ring(p._reg) : f.geometry,
                    properties: out,
                };
            }),
        };
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: "application/geo+json" }));
        a.download = `area_${task}_${job?.id || handoff?.name || "result"}.geojson`;
        a.click();
        URL.revokeObjectURL(a.href);
    };

    const list = tasks.length ? tasks : [{ id: "hisup", label: "Building delineation", note: "individual footprints" }];
    const chosen = list.find((t) => t.id === task) || list[0];

    return (
        <div className="space-y-2.5">
            <div className="flex items-center justify-between">
                <span className="text-[10px] font-medium uppercase tracking-wide" style={{ color: "var(--text-muted)" }}>
                    Run a model on this area
                </span>
                <Pill colour={gpu.ok ? "var(--ok)" : "var(--warn)"}>{gpu.ok ? "GPU" : "offline"}</Pill>
            </div>

            <select value={task} onChange={(e) => setTask(e.target.value)} disabled={running}
                    className="w-full rounded-md px-2 py-1.5 text-[11px]"
                    style={{ background: "var(--surface)", border: "1px solid var(--line)", color: "var(--text-primary)" }}>
                {list.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
            {chosen?.note && <p className="text-[10px]" style={{ color: "var(--text-dim)" }}>{chosen.note}</p>}

            {handoff && (
                <p className="rounded-md px-2 py-1.5 text-[10px]"
                   style={{ background: "var(--accent-dim)", border: "1px solid var(--accent)", color: "var(--text-primary)" }}>
                    Showing <span className="mono">{handoff.name}</span> from Analysis.
                </p>
            )}

            {!hasAoi && !running && !done ? (
                <p className="text-[11px]" style={{ color: "var(--text-dim)" }}>
                    Draw an area above first — the model runs inside it.
                </p>
            ) : !running ? (
                <Button variant="primary" className="w-full" onClick={start} disabled={!gpu.ok || !hasAoi}>
                    <Play size={13} /> {done ? "Run again" : "Run on this area"}
                </Button>
            ) : (
                <>
                    <div className="h-1.5 w-full overflow-hidden rounded-full" style={{ background: "var(--surface-2)" }}>
                        <div className="h-full transition-[width] duration-500"
                             style={{ width: `${job.percent || 0}%`, background: "var(--accent)" }} />
                    </div>
                    <div className="flex items-center justify-between text-[10px]" style={{ color: "var(--text-dim)" }}>
                        <span className="mono">
                            {job.status === "queued" ? "queued…" : `${job.percent ?? 0}%`}
                            {count ? ` · ${count} found` : ""}
                        </span>
                        <span className="mono">
                            {fmt(job.elapsed_s)}{job.eta_s != null ? ` · ~${fmt(job.eta_s)} left` : ""}
                        </span>
                    </div>
                    <Button className="w-full" onClick={cancel}><Square size={12} /> Stop</Button>
                </>
            )}

            {err && (
                <p className="rounded-md px-2 py-1.5 text-[10px]"
                   style={{ background: "var(--danger-dim)", border: "1px solid var(--danger)", color: "var(--danger)" }}>
                    {err}
                </p>
            )}

            {count > 0 && (
                <div className="space-y-2 rounded-md p-2" style={{ background: "var(--surface-2)", border: "1px solid var(--line)" }}>
                    <div className="flex items-center justify-between text-[11px]">
                        <span style={{ color: "var(--text-primary)" }}>
                            <span className="mono">{count.toLocaleString()}</span> buildings
                        </span>
                        {(done || !running) && (
                            <Button onClick={exportGeoJSON}><Download size={12} /> Export</Button>
                        )}
                    </div>
                    <label className="block">
                        <div className="flex items-center justify-between text-[10px]" style={{ color: "var(--text-dim)" }}>
                            <span>Overlay opacity</span><span className="mono">{Math.round(opacity * 100)}%</span>
                        </div>
                        <input type="range" min="0" max="1" step="0.05" value={opacity}
                               onChange={(e) => setOpacity(Number(e.target.value))}
                               className="w-full accent-[var(--accent)]" />
                    </label>

                    {/* ── heights, measured as the buildings arrive ── */}
                    {!heights && heightNote && (
                        <p className="text-[10px]" style={{ color: "var(--text-dim)" }}>
                            <Ruler size={10} className="mr-1 inline" />
                            No heights: {heightNote}.
                        </p>
                    )}

                    {heights?.measured > 0 && (
                        <div className="space-y-2 border-t pt-2" style={{ borderColor: "var(--line)" }}>
                            <div className="flex items-center justify-between text-[11px]">
                                <span style={{ color: "var(--text-primary)" }}>
                                    <span className="mono">{heights.mean_height_m ?? "—"}</span> m mean
                                    {heights.max_height_m != null && (
                                        <span style={{ color: "var(--text-dim)" }}>
                                            {" · "}<span className="mono">{heights.max_height_m}</span> m tallest
                                        </span>
                                    )}
                                </span>
                                <Pill colour="var(--signal)" mono>
                                    {heights.measured}/{heights.n}
                                    {running ? " …" : ""}
                                </Pill>
                            </div>

                            <label className="flex cursor-pointer items-center gap-2 text-[10px]"
                                   style={{ color: "var(--text-dim)" }}>
                                <input type="checkbox" checked={byFloors}
                                       onChange={(e) => setByFloors(e.target.checked)}
                                       className="accent-[var(--accent)]" />
                                Colour by storeys
                            </label>
                            <label className="flex cursor-pointer items-center gap-2 text-[10px]"
                                   style={{ color: "var(--text-dim)" }}>
                                <input type="checkbox" checked={labels}
                                       onChange={(e) => setLabels(e.target.checked)}
                                       className="accent-[var(--accent)]" />
                                Height on each building
                                <span style={{ color: "var(--text-mute)" }}>(zoom in)</span>
                            </label>

                            {byFloors && (
                                <div className="space-y-1">
                                    {FLOOR_COLOURS.map((c, i) => (
                                        <div key={c} className="flex items-center gap-2 text-[10px]">
                                            <span className="inline-block h-2.5 w-4 shrink-0 rounded-[2px]"
                                                  style={{ background: c, border: "1px solid var(--line)" }} />
                                            <span style={{ color: "var(--text-dim)" }}>{FLOOR_LABELS[i]}</span>
                                            <span className="mono ml-auto" style={{ color: "var(--text-mute)" }}>
                                                {heights.bins?.[i] || "—"}
                                            </span>
                                        </div>
                                    ))}
                                    {heights.measured < heights.n && (
                                        <div className="flex items-center gap-2 text-[10px]">
                                            <span className="inline-block h-2.5 w-4 shrink-0 rounded-[2px]"
                                                  style={{ background: UNMEASURED, border: "1px solid var(--line)" }} />
                                            <span style={{ color: "var(--text-dim)" }}>
                                                {running ? "measuring" : "not measured"}
                                            </span>
                                            <span className="mono ml-auto" style={{ color: "var(--text-mute)" }}>
                                                {heights.n - heights.measured}
                                            </span>
                                        </div>
                                    )}
                                </div>
                            )}

                            <p className="text-[10px]" style={{ color: "var(--text-mute)" }}>
                                Labels appear from zoom {LABEL_MINZOOM} — below that they would
                                be wider than the buildings. Click any building for its full
                                numbers at any zoom.
                            </p>

                            {done && job?.id && (
                                <Button className="w-full" onClick={saveHeights} disabled={saving}>
                                    <Ruler size={12} />
                                    {saving ? "Saving…" : "Save heights into the result file"}
                                </Button>
                            )}
                        </div>
                    )}
                </div>
            )}

            {done && count === 0 && !handoff && (
                <p className="text-[10px]" style={{ color: "var(--text-dim)" }}>
                    Finished, but nothing was found in that area.
                </p>
            )}
        </div>
    );
}
