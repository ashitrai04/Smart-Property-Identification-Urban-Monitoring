import { DRONE_DISTRICTS } from "./DroneLayers";
import { imageryBeforeId } from "../lib/mapOrder";
import { useCallback, useEffect, useRef, useState } from "react";
import { Crosshair, Image as ImageIcon, RefreshCw, ScanSearch } from "lucide-react";
import { gpuBase, probeGpu, useGpuStatus } from "../lib/modelApi";
import { Empty, Pill, SectionHeader, Spinner, ToggleRow } from "./ui";
import { GRID_BEFORE, clipNumber } from "./DroneGrid";

// Overlays sit under the Ongole drone grid so its boundary stays on top.
const below = (map) => (map.getLayer(GRID_BEFORE) ? GRID_BEFORE : undefined);

/*
 * Imagery and results held on the GPU server: compressed COG orthomosaics served
 * as map tiles, and finished jobs' outputs (vectors from the GeoPackage, loaded
 * for the visible area only; class maps as tiles). GPU-server only — there is no
 * Hugging Face fallback for 50 GB rasters.
 */
const RESULT_STYLE = {
    road_damage: { color: "#F5A524", label: "Road damage" },
    buildings: { color: "#FF4FD8", label: "Buildings" },
    roads: { color: "#22D3EE", label: "Roads" },
    waterbodies: { color: "#38BDF8", label: "Water" },
    openareas: { color: "#FBBF24", label: "Open areas" },
};
const TASK_LABEL = { pothole: "Road damage", segment: "Segmentation", fusion: "SAM fusion" };

function bboxOf(map) {
    const b = map.getBounds();
    return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(6)).join(",");
}

export default function ServerImagery({ getMap }) {
    const gpu = useGpuStatus();
    const [imagery, setImagery] = useState([]);
    const [jobs, setJobs] = useState([]);
    const [on, setOn] = useState({});          // layer key -> true
    const [busy, setBusy] = useState(null);
    const [err, setErr] = useState(null);
    const vectorKeys = useRef(new Map());      // key -> {jobId, layer}

    const load = useCallback(async () => {
        if (!gpuBase()) await probeGpu(true);
        const GPU_BASE = gpuBase();
        if (!GPU_BASE) return;
        setErr(null);
        if (!(await probeGpu(true))) { setErr("GPU server unreachable"); return; }
        try {
            const [im, jb] = await Promise.all([
                fetch(`${GPU_BASE}/imagery`).then((r) => r.json()),
                fetch(`${GPU_BASE}/jobs`).then((r) => r.json()),
            ]);
            // Ongole / Guntur drone imagery lives in its own drone section of the rail.
            setImagery(im.filter((i) => i.status === "ready" && !DRONE_DISTRICTS.includes((i.district || "").toLowerCase())));
            setJobs(jb);
        } catch (e) {
            setErr(String(e?.message || e));
        }
    }, []);
    useEffect(() => { load(); }, [load]);

    // Vector results follow the viewport: refetch the visible area after a pan / zoom.
    const reloadVectors = useCallback(async () => {
        const map = getMap();
        if (!map) return;
        const bbox = bboxOf(map), zoom = map.getZoom().toFixed(1);
        await Promise.all([...vectorKeys.current.entries()].map(async ([key, { jobId, layer }]) => {
            try {
                const gj = await (await fetch(`${gpuBase()}/results/${jobId}/${layer}.geojson?bbox=${bbox}&zoom=${zoom}`)).json();
                map.getSource(key)?.setData(gj);
            } catch { /* keep the last data */ }
        }));
    }, [getMap]);
    useEffect(() => {
        // The map is rebuilt on a base-map switch and the ref fills in only once it
        // has loaded, so wait for it rather than binding to nothing.
        let map = null, t, poll;
        const h = () => { clearTimeout(t); t = setTimeout(reloadVectors, 400); };
        const bind = () => {
            map = getMap();
            if (map) map.on("moveend", h);
            else poll = setTimeout(bind, 300);
        };
        bind();
        return () => { clearTimeout(t); clearTimeout(poll); map?.off("moveend", h); };
    }, [getMap, reloadVectors]);

    const removeKey = (map, key) => {
        for (const id of [`${key}-fill`, `${key}-line`, key]) if (map.getLayer(id)) map.removeLayer(id);
        if (map.getSource(key)) map.removeSource(key);
        vectorKeys.current.delete(key);
    };

    const toggleImagery = (img) => {
        const map = getMap();
        if (!map) return;
        const key = `srv-img-${img.id}`;
        if (on[key]) {
            removeKey(map, key);
            setOn((p) => ({ ...p, [key]: false }));
            return;
        }
        map.addSource(key, {
            type: "raster", tileSize: 256, bounds: img.bounds, maxzoom: 22,
            tiles: [`${gpuBase()}/imagery/${img.id}/tiles/{z}/{x}/{y}.webp`],
        });
        map.addLayer({ id: key, type: "raster", source: key, paint: { "raster-opacity": 0, "raster-opacity-transition": { duration: 600 } } }, imageryBeforeId(map));
        requestAnimationFrame(() => map.setPaintProperty(key, "raster-opacity", 1));
        setOn((p) => ({ ...p, [key]: true }));
        if (img.bounds) map.fitBounds([[img.bounds[0], img.bounds[1]], [img.bounds[2], img.bounds[3]]], { padding: 60, duration: 1400 });
    };

    const toggleResult = async (job, layer) => {
        const map = getMap();
        if (!map) return;
        const key = `srv-res-${job.id}-${layer}`;
        if (on[key]) {
            removeKey(map, key);
            setOn((p) => ({ ...p, [key]: false }));
            return;
        }
        if (layer === "classes") {
            map.addSource(key, { type: "raster", tileSize: 256, maxzoom: 22, tiles: [`${gpuBase()}/results/${job.id}/classes/{z}/{x}/{y}.png`] });
            map.addLayer({ id: key, type: "raster", source: key, paint: { "raster-opacity": 0.85 } }, below(map));
            setOn((p) => ({ ...p, [key]: true }));
            return;
        }
        setBusy(key);
        const st = RESULT_STYLE[layer] || { color: "#2DD4BF" };
        map.addSource(key, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        const potholeFill = ["case", ["==", ["get", "is_pothole"], true], "#EF4444", st.color];
        map.addLayer({ id: `${key}-fill`, type: "fill", source: key,
            paint: { "fill-color": layer === "road_damage" ? potholeFill : st.color, "fill-opacity": layer === "road_damage" ? 0.25 : 0.4 } }, below(map));
        map.addLayer({ id: `${key}-line`, type: "line", source: key,
            paint: { "line-color": layer === "road_damage" ? potholeFill : st.color, "line-width": layer === "road_damage" ? 2 : 1 } }, below(map));
        vectorKeys.current.set(key, { jobId: job.id, layer });
        setOn((p) => ({ ...p, [key]: true }));
        await reloadVectors();
        setBusy(null);
    };

    const fly = (img) => {
        const map = getMap();
        if (map && img.bounds) map.fitBounds([[img.bounds[0], img.bounds[1]], [img.bounds[2], img.bounds[3]]], { padding: 60, duration: 1400 });
    };

    if (!gpu.base) return null;

    return (
        <>
            <SectionHeader right={
                <span className="flex items-center gap-1.5">
                    <Pill colour={gpu.ok ? "var(--ok)" : "var(--alert)"}>{gpu.ok ? "GPU" : "offline"}</Pill>
                    <button onClick={load} title="Refresh" className="rounded-[5px] p-0.5 hover:bg-[var(--surface-2)]" style={{ color: "var(--text-mute)" }}>
                        <RefreshCw size={11} />
                    </button>
                </span>
            }>Server imagery</SectionHeader>
            <div className="px-1.5 pb-1">
                {err && <Empty>{err}. Imagery and batch results come from the GPU server only.</Empty>}
                {!err && !imagery.length && <Empty>No imagery ingested yet. On the server: <span className="mono">spctl.py ingest …</span></Empty>}
                {imagery.map((img) => {
                    const key = `srv-img-${img.id}`;
                    const done = jobs.filter((j) => j.imagery_id === img.id && j.status === "done");
                    const running = jobs.filter((j) => j.imagery_id === img.id && j.status === "running");
                    return (
                        <div key={img.id} className="mb-1">
                            <div className="flex items-center">
                                <div className="min-w-0 flex-1">
                                    <ToggleRow on={!!on[key]} onClick={() => toggleImagery(img)} colour="#2DD4BF"
                                        label={clipNumber(img) && (img.district || "").toLowerCase() === "ongole" ? `Ongole · Clip ${String(clipNumber(img)).padStart(2, "0")}` : img.name}
                                        sub={`${img.kind}${img.gsd_m ? ` · ${(img.gsd_m * 100).toFixed(1)} cm` : ""}${img.cog_bytes ? ` · ${(img.cog_bytes / 2 ** 30).toFixed(1)} GB` : ""}`}
                                        icon={<ImageIcon size={12} style={{ color: "var(--signal)" }} />} />
                                </div>
                                <button onClick={() => fly(img)} title="Fly to" className="mr-1 rounded-[5px] p-1 hover:bg-[var(--surface-2)]" style={{ color: "var(--text-mute)" }}>
                                    <Crosshair size={12} />
                                </button>
                            </div>
                            {running.map((j) => (
                                <div key={j.id} className="ml-6 flex items-center gap-1.5 py-1 text-[10.5px]" style={{ color: "var(--text-dim)" }}>
                                    <Spinner size={10} /> {TASK_LABEL[j.task]} · {Math.round(100 * (j.progress || 0))}%
                                </div>
                            ))}
                            {done.map((j) => (
                                <div key={j.id} className="ml-4">
                                    {Object.entries(j.layers || {}).map(([layer, n]) => {
                                        const rk = `srv-res-${j.id}-${layer}`;
                                        const st = RESULT_STYLE[layer] || { color: "#2DD4BF", label: layer };
                                        return (
                                            <ToggleRow key={rk} on={!!on[rk]} onClick={() => toggleResult(j, layer)} colour={st.color}
                                                label={`${st.label} · ${n.toLocaleString()}`} sub={TASK_LABEL[j.task]}
                                                icon={busy === rk ? <Spinner size={11} /> : <ScanSearch size={12} style={{ color: st.color }} />} />
                                        );
                                    })}
                                    {(j.task === "segment" || j.task === "fusion") && (
                                        <ToggleRow on={!!on[`srv-res-${j.id}-classes`]} onClick={() => toggleResult(j, "classes")} colour="#EF4444"
                                            label="Class map" sub={TASK_LABEL[j.task]} icon={<ScanSearch size={12} style={{ color: "#EF4444" }} />} />
                                    )}
                                </div>
                            ))}
                        </div>
                    );
                })}
            </div>
        </>
    );
}
