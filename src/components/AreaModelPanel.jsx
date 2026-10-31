import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Play, Square } from "lucide-react";
import { Button, Pill, Spinner } from "./ui";
import { gpuOnlyFetch, useGpuStatus } from "../lib/modelApi";
import { logActivity } from "../lib/activityLog";
import { takeFromMap } from "../lib/mapHandoff";

/*
 * Run a model over the polygon drawn on the map.
 *
 * The Analysis page answers "what is in this image I am uploading". This
 * answers "what is in this piece of ground", which is the question a survey
 * actually asks — and it needs no upload, because the orthomosaic is already
 * on the server. The area is divided into windows there; the only thing the
 * operator chooses is the model and where.
 *
 * Progress matters more here than anywhere else in the app: an area job is
 * minutes, not seconds, and without a bar it is indistinguishable from a
 * hang. The server reports completed windows, so the bar is real rather than
 * an animation, and the estimate is elapsed/progress — honest because every
 * window costs about the same.
 */
const SRC = "area-model-src";
const FILL = "area-model-fill";
const LINE = "area-model-line";

// One colour per layer a model can return; buildings is the common case.
const LAYER_COLOUR = {
    buildings: "#22c55e",
    roads: "#eab308",
    waterbodies: "#38bdf8",
    openareas: "#a3a3a3",
};
const FALLBACK = "#22c55e";

function colourExpression() {
    const stops = [];
    for (const [k, v] of Object.entries(LAYER_COLOUR)) stops.push(k, v);
    return ["match", ["coalesce", ["get", "layer"], "buildings"], ...stops, FALLBACK];
}

function fmt(s) {
    if (s == null) return "—";
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

export default function AreaModelPanel({ map, aoiFeatures }) {
    const [tasks, setTasks] = useState([]);
    const [task, setTask] = useState("hisup");
    const [job, setJob] = useState(null);       // { id, status, percent, eta_s, ... }
    const [result, setResult] = useState(null);
    const [opacity, setOpacity] = useState(0.55);
    const [err, setErr] = useState("");
    const pollRef = useRef(null);
    const gpu = useGpuStatus();

    const hasAoi = Array.isArray(aoiFeatures) && aoiFeatures.length > 0;
    const running = job && (job.status === "queued" || job.status === "running");

    // The model list comes from the server so it cannot drift from what runs.
    useEffect(() => {
        let gone = false;
        gpuOnlyFetch("/area/tasks")
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (!gone && d?.tasks?.length) setTasks(d.tasks); })
            .catch(() => { /* offline; the panel already says so */ });
        return () => { gone = true; };
    }, [gpu.ok]);

    // ── the result layer ────────────────────────────────────────
    const paint = useCallback(() => {
        if (!map) return;
        try {
            map.setPaintProperty(FILL, "fill-opacity", opacity);
            map.setPaintProperty(LINE, "line-opacity", Math.min(1, opacity + 0.35));
        } catch { /* layer not added yet */ }
    }, [map, opacity]);

    useEffect(() => { paint(); }, [paint]);

    const clearLayer = useCallback(() => {
        if (!map) return;
        for (const id of [FILL, LINE]) { try { map.getLayer(id) && map.removeLayer(id); } catch { /* gone */ } }
        try { map.getSource(SRC) && map.removeSource(SRC); } catch { /* gone */ }
    }, [map]);

    const showOnMap = useCallback((geojson) => {
        if (!map || !geojson) return;
        clearLayer();
        map.addSource(SRC, { type: "geojson", data: geojson });
        map.addLayer({
            id: FILL, type: "fill", source: SRC,
            paint: { "fill-color": colourExpression(), "fill-opacity": opacity },
        });
        map.addLayer({
            id: LINE, type: "line", source: SRC,
            paint: { "line-color": colourExpression(), "line-width": 1.2,
                     "line-opacity": Math.min(1, opacity + 0.35) },
        });
    }, [map, opacity, clearLayer]);

    // Removing the layer when the panel goes away avoids leaving an
    // unremovable overlay behind on a style rebuild.
    useEffect(() => () => { clearLayer(); if (pollRef.current) clearInterval(pollRef.current); }, [clearLayer]);

    const [handoff, setHandoff] = useState(null);

    // A georeferenced upload sent over from the Analysis page lands here: it
    // is already world-coordinate GeoJSON, so it needs the overlay and the
    // opacity slider but no job and no progress bar.
    useEffect(() => {
        if (!map) return;
        const d = takeFromMap();
        if (!d) return;
        setHandoff(d);
        setResult(d.geojson);
        showOnMap(d.geojson);
        try {
            const xs = [], ys = [];
            for (const f of d.geojson.features) {
                for (const [x, y] of f.geometry.coordinates[0]) { xs.push(x); ys.push(y); }
            }
            if (xs.length) {
                map.fitBounds([[Math.min(...xs), Math.min(...ys)],
                               [Math.max(...xs), Math.max(...ys)]], { padding: 60, duration: 800 });
            }
        } catch { /* a bad ring should not stop the overlay showing */ }
    }, [map, showOnMap]);

    // ── running ─────────────────────────────────────────────────
    const poll = useCallback((id) => {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = setInterval(async () => {
            try {
                const r = await gpuOnlyFetch(`/area/${id}`);
                if (!r.ok) return;
                const d = await r.json();
                setJob(d);
                if (d.status === "done" || d.status === "failed" || d.status === "cancelled") {
                    clearInterval(pollRef.current); pollRef.current = null;
                    if (d.status === "failed") setErr(d.message || "the job failed");
                    if (d.status === "done") {
                        const g = await (await gpuOnlyFetch(`/area/${id}/result.geojson`)).json();
                        setResult(g);
                        showOnMap(g);
                        logActivity?.({ kind: "model", label: `Area run — ${g.features?.length || 0} features` });
                    }
                }
            } catch { /* transient; the next tick retries */ }
        }, 2000);
    }, [showOnMap]);

    const start = async () => {
        if (!hasAoi) return;
        setErr(""); setResult(null); setJob(null); clearLayer();
        // Several drawn polygons are one area as far as the server is
        // concerned, so send their union as a MultiPolygon.
        const coords = aoiFeatures.map((f) => f.geometry.coordinates);
        const aoi = aoiFeatures.length === 1
            ? aoiFeatures[0]
            : { type: "Feature", properties: {},
                geometry: { type: "MultiPolygon", coordinates: coords } };
        try {
            const r = await gpuOnlyFetch("/area", {
                method: "POST",
                headers: { "content-type": "application/json" },
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
    };

    const download = () => {
        const blob = new Blob([JSON.stringify(result, null, 1)], { type: "application/geo+json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `area_${task}_${job?.id || "result"}.geojson`;
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

            <select
                value={task}
                onChange={(e) => setTask(e.target.value)}
                disabled={running}
                className="w-full rounded-md px-2 py-1.5 text-[11px]"
                style={{ background: "var(--surface)", border: "1px solid var(--line)", color: "var(--text-primary)" }}
            >
                {list.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
            {chosen?.note && (
                <p className="text-[10px]" style={{ color: "var(--text-dim)" }}>{chosen.note}</p>
            )}

            {handoff && (
                <p className="rounded-md px-2 py-1.5 text-[10px]"
                   style={{ background: "var(--accent-dim)", border: "1px solid var(--accent)", color: "var(--text-primary)" }}>
                    Showing <span className="mono">{handoff.name}</span> from Analysis.
                </p>
            )}

            {!hasAoi ? (
                <p className="text-[11px]" style={{ color: "var(--text-dim)" }}>
                    Draw an area above first — the model runs inside it.
                </p>
            ) : !running ? (
                <Button variant="primary" className="w-full" onClick={start} disabled={!gpu.ok}>
                    <Play size={13} /> Run on this area
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
                            {job.windows ? ` · ${job.windows} windows` : ""}
                        </span>
                        <span className="mono">
                            {fmt(job.elapsed_s)} elapsed{job.eta_s != null ? ` · ~${fmt(job.eta_s)} left` : ""}
                        </span>
                    </div>
                    <Button className="w-full" onClick={cancel}>
                        <Square size={12} /> Stop
                    </Button>
                </>
            )}

            {err && (
                <p className="rounded-md px-2 py-1.5 text-[10px]"
                   style={{ background: "var(--danger-dim)", border: "1px solid var(--danger)", color: "var(--danger)" }}>
                    {err}
                </p>
            )}

            {result && (
                <div className="space-y-2 rounded-md p-2" style={{ background: "var(--surface-2)", border: "1px solid var(--line)" }}>
                    <div className="flex items-center justify-between text-[11px]">
                        <span style={{ color: "var(--text-primary)" }}>
                            <span className="mono">{(result.features?.length || 0).toLocaleString()}</span> features
                        </span>
                        <Button onClick={download}><Download size={12} /> GeoJSON</Button>
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

            {job?.status === "done" && !result?.features?.length && (
                <p className="text-[10px]" style={{ color: "var(--text-dim)" }}>
                    Finished, but nothing was found in that area.
                </p>
            )}
        </div>
    );
}
