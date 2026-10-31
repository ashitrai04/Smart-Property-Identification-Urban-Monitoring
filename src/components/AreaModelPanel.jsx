import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Play, Square } from "lucide-react";
import { Button, Pill } from "./ui";
import { gpuOnlyFetch, useGpuStatus } from "../lib/modelApi";
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
            paint: { "fill-color": ["get", "colour"], "fill-opacity": opacity },
        });
        map.addLayer({
            id: LINE, type: "line", source: SRC,
            paint: {
                "line-color": ["get", "colour"],
                // A building still showing the detector's outline is drawn
                // thicker, so the eye is drawn to what just happened.
                "line-width": ["case", ["==", ["get", "stage"], "raw"], 2.6, 1.2],
                "line-opacity": Math.min(1, opacity + 0.35),
            },
        });
    }, [map, opacity]);

    useEffect(() => {
        if (!map) return;
        try {
            map.setPaintProperty(FILL, "fill-opacity", opacity);
            map.setPaintProperty(LINE, "line-opacity", Math.min(1, opacity + 0.35));
        } catch { /* layer not added yet */ }
    }, [map, opacity]);

    const clearLayer = useCallback(() => {
        if (!map) return;
        for (const id of [FILL, LINE]) { try { map.getLayer(id) && map.removeLayer(id); } catch { /* gone */ } }
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
            properties: { ...f.properties, colour: nextColour(), stage: "reg", t: 0, id: i },
        }));
        setCount(featsRef.current.length);
        setDone(true);
        push();
        try {
            const xs = [], ys = [];
            for (const f of featsRef.current) for (const [x, y] of f.geometry.coordinates[0]) { xs.push(x); ys.push(y); }
            if (xs.length) map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]],
                                         { padding: 60, duration: 800 });
        } catch { /* a bad ring should not stop the overlay showing */ }
    }, [map, push]);

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
                    for (const it of d.items) {
                        featsRef.current.push({
                            type: "Feature",
                            geometry: ring(it.raw),       // as detected; settles to `reg`
                            properties: {
                                colour: nextColour(), stage: "raw", t: Date.now(),
                                _reg: it.reg, area_m2: it.area_m2,
                            },
                        });
                    }
                    setCount(featsRef.current.length);
                    push();
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
    }, [push]);

    const start = async () => {
        if (!hasAoi) return;
        setErr(""); setDone(false); setJob(null); setCount(0); setHandoff(null);
        clearLayer();
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

    const exportGeoJSON = () => {
        // Export what the pipeline settled on, not the in-flight outline.
        const out = {
            type: "FeatureCollection",
            features: featsRef.current.map((f, i) => ({
                type: "Feature",
                geometry: f.properties._reg ? ring(f.properties._reg) : f.geometry,
                properties: { building_id: i + 1, area_m2: f.properties.area_m2 ?? null },
            })),
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
