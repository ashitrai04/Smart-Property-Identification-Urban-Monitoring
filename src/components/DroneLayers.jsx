import { useCallback, useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import { Crosshair, Droplets, Image as ImageIcon, LandPlot, Route, ScanSearch, Building2 } from "lucide-react";
import { gpuBase, probeGpu, useGpuStatus } from "../lib/modelApi";
import { GRID_BEFORE, clipNumber } from "./DroneGrid";
import { imageryBeforeId } from "../lib/mapOrder";
import { Empty, Spinner, ToggleRow } from "./ui";

/*
 * Drone imagery of a district (Ongole, Guntur) + its detection layers, in the left rail as ordinary layers:
 *   road damage (road-gated potholes / cracks), buildings, roads, water bodies, open areas.
 * Everything comes from the GPU server: the latest finished job of each kind for the
 * imagery. Vectors load for the visible area only, from a sensible zoom, and sit beneath
 * the Ongole grid so its boundary stays on top.
 */
const DETECTIONS = [
    // "refine" = seamless, cleaned, SAM-sharpened version of a segmentation (+ re-checked potholes);
    // the newest finished job wins, so refined layers replace the raw ones once ready.
    // "gtgate" = road damage kept to the district's ground-truth roads (newest wins)
    { layer: "road_damage", label: "Road damage", sub: "potholes & cracks on roads", color: "#F5A524", task: ["pothole", "refine", "gtgate"], minzoom: 14, icon: ScanSearch },
    { layer: "buildings", label: "Buildings", color: "#FF4FD8", task: ["fusion", "segment", "refine"], minzoom: 15, icon: Building2 },
    { layer: "roads", label: "Roads", color: "#F1F5F9", task: ["segment", "fusion", "refine"], minzoom: 13, icon: Route },
    { layer: "waterbodies", label: "Water bodies", color: "#38BDF8", task: ["segment", "fusion", "refine"], minzoom: 12, icon: Droplets },
    { layer: "openareas", label: "Open areas", color: "#FBBF24", task: ["segment", "refine"], minzoom: 14, icon: LandPlot },
];
// Districts with their own drone section in the rail (kept out of "Server imagery").
// Change detection between two dates (task "change", stored on the newer image).
const CHANGES = [
    { layer: "new_buildings", label: "New buildings", color: "#22C55E", task: ["change"], minzoom: 13, icon: Building2 },
    { layer: "removed_buildings", label: "Demolished buildings", color: "#EF4444", task: ["change"], minzoom: 13, icon: Building2 },
    { layer: "new_roads", label: "New roads", color: "#EAB308", task: ["change"], minzoom: 13, icon: Route },
    { layer: "water_gained", label: "Water gained", color: "#38BDF8", task: ["change"], minzoom: 12, icon: Droplets },
    { layer: "water_lost", label: "Water lost", color: "#B45309", task: ["change"], minzoom: 12, icon: Droplets },
];

export const DRONE_DISTRICTS = ["ongole", "guntur"];
const below = (map) => (map.getLayer(GRID_BEFORE) ? GRID_BEFORE : undefined);

function bbox(map) {
    const b = map.getBounds();
    return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(6)).join(",");
}

export default function DroneLayers({ getMap, district = "ongole", kind = "drone" }) {
    const sat = kind === "satellite";
    const dets = sat ? DETECTIONS.filter((d) => d.layer !== "road_damage") : DETECTIONS;   // no potholes at 30 cm
    const gpu = useGpuStatus();
    const [images, setImages] = useState([]);
    const [preparing, setPreparing] = useState([]);   // still being imported / compressed
    const [jobs, setJobs] = useState([]);
    const [on, setOn] = useState({});
    const [busy, setBusy] = useState(null);
    const [zoom, setZoom] = useState(0);
    const active = useRef(new Map());          // source id -> {url, minzoom}

    useEffect(() => {
        let live = true;
        (async () => {
            if (!(await probeGpu())) return;
            try {
                const [im, jb] = await Promise.all([
                    fetch(`${gpuBase()}/imagery`).then((r) => r.json()),
                    fetch(`${gpuBase()}/jobs`).then((r) => r.json()),
                ]);
                if (!live) return;
                const mine = im.filter((i) => (i.district || "").toLowerCase() === district
                    && (sat ? i.kind === "satellite" : i.kind !== "satellite"));
                setImages(mine.filter((i) => i.status === "ready")
                    .sort(sat ? (a, b) => String(a.captured || a.name).localeCompare(String(b.captured || b.name))   // oldest first
                              : (a, b) => (b.cog_bytes || 0) - (a.cog_bytes || 0)));   // full mosaic first
                setPreparing(mine.filter((i) => i.status === "ingesting"));
                setJobs(jb);
            } catch { /* offline */ }
        })();
        return () => { live = false; };
    }, [gpu.base, gpu.ok, district, sat]);

    // Latest finished job (of the given kinds) for an image that produced `layer`.
    const jobFor = (img, det) => jobs
        .filter((j) => j.imagery_id === img.id && j.status === "done" && det.task.includes(j.task) && (j.layers || {})[det.layer])
        .sort((a, b) => (b.finished || 0) - (a.finished || 0))[0];
    const runningFor = (img) => jobs.filter((j) => j.imagery_id === img.id && (j.status === "running" || j.status === "queued"));

    // Viewport reload for every active vector layer.
    const reload = useCallback(async () => {
        const map = getMap();
        if (!map) return;
        const z = map.getZoom();
        setZoom(z);
        await Promise.all([...active.current.entries()].map(async ([id, { url, minzoom }]) => {
            const src = map.getSource(id);
            if (!src) return;
            if (z < minzoom) { src.setData({ type: "FeatureCollection", features: [] }); return; }
            try {
                src.setData(await (await fetch(`${url}${url.includes("?") ? "&" : "?"}bbox=${bbox(map)}&zoom=${z.toFixed(1)}`)).json());
            } catch { /* keep last */ }
        }));
    }, [getMap]);

    useEffect(() => {
        let map = null, t, poll;
        const h = () => { clearTimeout(t); t = setTimeout(reload, 350); };
        const bind = () => { map = getMap(); if (map) { map.on("moveend", h); setZoom(map.getZoom()); } else poll = setTimeout(bind, 300); };
        bind();
        return () => { clearTimeout(t); clearTimeout(poll); map?.off("moveend", h); };
    }, [getMap, reload]);

    const clicks = useRef(new Map());        // layer id -> click handler (detached on remove)
    const remove = (map, id) => {
        const h = clicks.current.get(id);
        if (h) { map.off("click", `${id}-fill`, h); clicks.current.delete(id); }
        for (const l of [`${id}-fill`, `${id}-line`, id]) if (map.getLayer(l)) map.removeLayer(l);
        if (map.getSource(id)) map.removeSource(id);
        active.current.delete(id);
    };

    const toggleImage = (img) => {
        const map = getMap();
        if (!map) return;
        const id = `srv-img-${img.id}`;
        if (on[id]) { remove(map, id); setOn((p) => ({ ...p, [id]: false })); return; }
        map.addSource(id, { type: "raster", tileSize: 256, bounds: img.bounds, maxzoom: 22,
            tiles: [`${gpuBase()}/imagery/${img.id}/tiles/{z}/{x}/{y}.webp`] });
        // imagery sits right on the basemap; every other layer stays above it
        map.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": 0, "raster-opacity-transition": { duration: 600 } } }, imageryBeforeId(map));
        requestAnimationFrame(() => map.setPaintProperty(id, "raster-opacity", 1));
        setOn((p) => ({ ...p, [id]: true }));
    };

    const toggleDet = async (img, det, job, offRoad = false) => {
        const map = getMap();
        if (!map || !job) return;
        const id = `drone-det-${img.id}-${det.layer}${offRoad ? "-off" : ""}`;
        if (on[id]) { remove(map, id); setOn((p) => ({ ...p, [id]: false })); return; }
        setBusy(id);
        const color = offRoad ? "#FEF08A" : det.color;
        map.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        const isDamage = det.layer === "road_damage";
        const fill = isDamage && !offRoad ? ["case", ["==", ["get", "is_pothole"], 1], "#EF4444", color] : color;
        map.addLayer({ id: `${id}-fill`, type: "fill", source: id,
            paint: { "fill-color": fill, "fill-opacity": offRoad ? 0.12 : isDamage ? 0.22 : det.layer === "roads" ? 0.5 : 0.4 } }, below(map));
        map.addLayer({ id: `${id}-line`, type: "line", source: id,
            paint: { "line-color": fill, "line-width": isDamage ? 2 : 1, ...(offRoad ? { "line-dasharray": [2, 2] } : {}) } }, below(map));
        const q = isDamage ? `?on_road=${offRoad ? 0 : 1}` : "";
        active.current.set(id, { url: `${gpuBase()}/results/${job.id}/${det.layer}.geojson${q}`, minzoom: det.minzoom });
        setOn((p) => ({ ...p, [id]: true }));
        // detection click → details
        const onClick = (e) => {
            const p = e.features?.[0]?.properties || {};
            const rows = isDamage
                ? [["Type", p.label], ["Confidence", p.confidence], ["Size", `${p.size_m} m`], ["On road", p.on_road ? "yes" : `no (${Math.round(100 * (p.road_overlap || 0))}% on road)`]]
                : [["Area", p.area_m2 ? `${p.area_m2} m²` : "—"],
                   ...(p.outline ? [["Outline", p.outline === "sam" ? `SAM-snapped (score ${p.sam_score})` : "SegFormer (cleaned)"]] : [])];
            new mapboxgl.Popup({ maxWidth: "240px" }).setLngLat(e.lngLat)
                .setHTML(`<div class="popup-title">${det.label}</div>${rows.map(([k, v]) => `<div class="popup-row"><span class="popup-key">${k}</span><span class="popup-value">${v ?? "—"}</span></div>`).join("")}`)
                .addTo(map);
        };
        map.on("click", `${id}-fill`, onClick);
        clicks.current.set(id, onClick);
        await reload();
        setBusy(null);
    };

    const fly = (img) => {
        const map = getMap();
        if (map && img.bounds) map.fitBounds([[img.bounds[0], img.bounds[1]], [img.bounds[2], img.bounds[3]]], { padding: 50, duration: 1400 });
    };

    if (!gpu.base) return null;
    const name = district[0].toUpperCase() + district.slice(1);
    const what = sat ? "satellite" : "drone";
    if (!images.length) return <Empty>{preparing.length ? `${name} ${what} imagery is being prepared on the GPU server…` : `No ${name} ${what} imagery on the GPU server yet.`}</Empty>;

    return (
        <div className="px-1.5 pb-1">
            {images.map((img) => {
                const imgId = `srv-img-${img.id}`;
                const run = runningFor(img);
                const gb = (img.cog_bytes || 0) / 2 ** 30;
                return (
                    <div key={img.id} className="mb-1">
                        <div className="flex items-center">
                            <div className="min-w-0 flex-1">
                                <ToggleRow on={!!on[imgId]} onClick={() => toggleImage(img)} colour="#2DD4BF"
                                    label={sat ? `Satellite · ${img.captured || img.name}`
                                        : clipNumber(img) ? `Drone imagery · Clip ${String(clipNumber(img)).padStart(2, "0")}` : `Drone imagery · ${img.name}`}
                                    sub={`${(img.gsd_m * 100).toFixed(1)} cm · ${gb >= 1 ? gb.toFixed(1) + " GB" : Math.round(gb * 1024) + " MB"}`}
                                    icon={<ImageIcon size={12} style={{ color: "#2DD4BF" }} />} />
                            </div>
                            <button onClick={() => fly(img)} title="Fly to" className="mr-1 rounded-[5px] p-1 hover:bg-[var(--surface-2)]" style={{ color: "var(--text-mute)" }}>
                                <Crosshair size={12} />
                            </button>
                        </div>

                        <div className="ml-3 mt-0.5 border-l pl-1.5" style={{ borderColor: "var(--line)" }}>
                            <div className="px-2 pb-0.5 pt-1 text-[10px] uppercase tracking-[0.09em]" style={{ color: "var(--text-mute)" }}>Detections</div>
                            {dets.map((det) => {
                                const job = jobFor(img, det);
                                const id = `drone-det-${img.id}-${det.layer}`;
                                const n = job ? (det.layer === "road_damage" ? job.stats?.on_road ?? job.layers?.road_damage : job.layers?.[det.layer]) : null;
                                const refined = job?.task === "refine";
                                const gt = job?.task === "gtgate";
                                const Icon = det.icon;
                                const pending = run.some((j) => det.task.includes(j.task));
                                return (
                                    <ToggleRow key={det.layer} on={!!on[id]} disabled={!job} onClick={() => toggleDet(img, det, job)} colour={det.color}
                                        label={det.label}
                                        sub={!job ? (pending ? "processing on the GPU…" : "not run yet")
                                            : on[id] && zoom < det.minzoom ? `zoom in to see (level ${det.minzoom}+)`
                                            : `${(n ?? 0).toLocaleString()}${gt ? " · on ground-truth roads" : det.sub ? " · " + det.sub : ""}${refined ? " · refined" : ""}`}
                                        icon={busy === id ? <Spinner size={11} /> : <Icon size={12} style={{ color: det.color }} />} />
                                );
                            })}
                            {!sat && (() => {
                                const det = DETECTIONS[0], job = jobFor(img, det);
                                const off = job?.stats?.filtered_off_road;
                                if (!job || !off) return null;
                                const id = `drone-det-${img.id}-${det.layer}-off`;
                                return (
                                    <ToggleRow on={!!on[id]} onClick={() => toggleDet(img, det, job, true)} colour="#FEF08A"
                                        label="Filtered out (off road)" sub={`${off.toLocaleString()} ${job.task === "gtgate" ? "off ground-truth roads" : "on fields, roofs, plots"} — for checking`}
                                        icon={<ScanSearch size={12} style={{ color: "#FEF08A" }} />} />
                                );
                            })()}
                            {(() => {
                                const det = { layer: "waterbodies_removed", label: "Removed water", color: "#94A3B8", task: ["refine"], minzoom: 12 };
                                const job = jobFor(img, det);
                                if (!job) return null;
                                const id = `drone-det-${img.id}-${det.layer}`;
                                return (
                                    <ToggleRow on={!!on[id]} onClick={() => toggleDet(img, det, job)} colour={det.color}
                                        label="Removed water (not water)" sub={`${(job.layers.waterbodies_removed || 0).toLocaleString()} fields, roofs, scrub — for checking`}
                                        icon={<Droplets size={12} style={{ color: det.color }} />} />
                                );
                            })()}
                            {(jobs.some((j) => j.imagery_id === img.id && j.task === "change")) && (
                                <>
                                    <div className="px-2 pb-0.5 pt-2 text-[10px] uppercase tracking-[0.09em]" style={{ color: "var(--text-mute)" }}>
                                        Change since {jobs.find((j) => j.imagery_id === img.id && j.task === "change")?.stats?.before || "the earlier date"}
                                    </div>
                                    {CHANGES.map((det) => {
                                        const job = jobFor(img, det);
                                        const id = `drone-det-${img.id}-${det.layer}`;
                                        const Icon = det.icon;
                                        const area = job?.stats?.area_m2?.[det.layer];
                                        return (
                                            <ToggleRow key={det.layer} on={!!on[id]} disabled={!job} onClick={() => toggleDet(img, det, job)} colour={det.color}
                                                label={det.label}
                                                sub={!job ? "comparing the two dates…"
                                                    : `${(job.layers?.[det.layer] || 0).toLocaleString()}${area ? ` · ${(area / 1e4).toFixed(1)} ha` : ""}`}
                                                icon={busy === id ? <Spinner size={11} /> : <Icon size={12} style={{ color: det.color }} />} />
                                        );
                                    })}
                                </>
                            )}
                            {run.length > 0 && (
                                <div className="flex items-center gap-1.5 px-2 py-1 text-[10.5px]" style={{ color: "var(--text-dim)" }}>
                                    <Spinner size={10} /> {run.map((j) => `${j.task} ${Math.round(100 * (j.progress || 0))}%`).join(" · ")}
                                </div>
                            )}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}
