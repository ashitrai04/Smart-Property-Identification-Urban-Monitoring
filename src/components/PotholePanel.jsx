import { useEffect, useRef, useState } from "react";
import { fromBlob } from "geotiff";
import { Download, ImagePlus, Play, RotateCcw, Square, X } from "lucide-react";
import { logActivity } from "../lib/activityLog";
import { HF_MODEL_BASE, modelFetch, probeGpu } from "../lib/modelApi";
import { Button, Card, Pill, SectionHeader, Spinner } from "./ui";
import { fetchFile, registerTour, unregisterTour } from "../tour/tourBus";

// YOLO road-damage detector (potholes + cracks): our own final_best.pt, served on
// CPU by the SegFormer Space at /pothole/* (no ZeroGPU quota, no third-party Space).
// GPU server first, Hugging Face Space as fallback (see lib/modelApi.js).
const MODES = ["auto", "manual"];
const ACCEPT = /\.(jpe?g|png|webp|bmp|tiff?)$/i;
const MAX_MB = 25;

// Wake the Space (and load the model) while the user is still choosing files.
let warmPromise = null;
function warm() {
    if (!warmPromise) warmPromise = Promise.all([
        probeGpu(true),
        fetch(`${HF_MODEL_BASE}/pothole/health`, { cache: "no-store" }).catch(() => { warmPromise = null; }),
    ]);
    return warmPromise;
}

async function detect(file, mode, conf, roadsOnly, signal) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("mode", mode);
    fd.append("conf", String(conf));
    fd.append("roads_only", roadsOnly ? "true" : "false");   // GPU server: drop detections off the road
    const r = await modelFetch("/pothole/detect", { method: "POST", body: fd, signal });
    if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
        if (r.status === 404) detail = "The pothole endpoint isn't available on the model Space yet (it may still be rebuilding).";
        if (r.status === 503 || r.status === 502) detail = "The model Space is starting up — try again in a minute.";
        throw new Error(detail);
    }
    return r.json();
}

const MAX_SIDE = 4096;   // larger rasters are downscaled while decoding

/* Trust the bytes, not the extension: a ".tif" is often something else — e.g.
   a PNG mask saved under the source image's name. TIFF starts "II*\0" or "MM\0*". */
async function isRealTiff(file) {
    const b = new Uint8Array(await file.slice(0, 4).arrayBuffer());
    return (b[0] === 0x49 && b[1] === 0x49 && b[2] === 0x2a && b[3] === 0x00)
        || (b[0] === 0x4d && b[1] === 0x4d && b[2] === 0x00 && b[3] === 0x2a);
}

const SIGNATURES = [
    [[0x89, 0x50, 0x4e, 0x47], "image/png", "png"],
    [[0xff, 0xd8, 0xff], "image/jpeg", "jpg"],
    [[0x52, 0x49, 0x46, 0x46], "image/webp", "webp"],
    [[0x42, 0x4d], "image/bmp", "bmp"],
];

/* A mislabelled file: re-wrap it with its real type so the preview and the
   upload both describe what it actually is. */
async function asImageFile(file) {
    const b = new Uint8Array(await file.slice(0, 4).arrayBuffer());
    const sig = SIGNATURES.find(([magic]) => magic.every((v, i) => b[i] === v));
    if (!sig) throw new Error("not a TIFF, PNG, JPEG, WEBP or BMP image");
    const [, type, ext] = sig;
    return new File([file], file.name.replace(/\.[^.]+$/, `.${ext}`), { type });
}

/* Browsers can't show TIFF in <img> (only Safari can), and a raw GeoTIFF is a
   heavy, fragile thing to post to the model. Decode it here to an 8-bit RGB
   PNG, used for both the preview and the model input. */
async function tiffToPng(file) {
    const image = await (await fromBlob(file)).getImage();
    const w0 = image.getWidth(), h0 = image.getHeight();
    const k = Math.min(1, MAX_SIDE / Math.max(w0, h0));
    const width = Math.max(1, Math.round(w0 * k)), height = Math.max(1, Math.round(h0 * k));
    const bands = image.getSamplesPerPixel();
    const pick = bands >= 3 ? [0, 1, 2] : [0];
    const rasters = await image.readRasters({ samples: pick, width, height, resampleMethod: "bilinear" });

    // 16-bit / float imagery: stretch each band's 2-98 % range to 0-255.
    const is8 = rasters[0] instanceof Uint8Array || rasters[0] instanceof Uint8ClampedArray;
    const scale = rasters.map((b) => {
        if (is8) return [0, 1];
        const sample = [];
        const step = Math.max(1, Math.floor(b.length / 20000));
        for (let i = 0; i < b.length; i += step) if (Number.isFinite(b[i]) && b[i] !== 0) sample.push(b[i]);
        sample.sort((x, y) => x - y);
        const lo = sample[Math.floor(sample.length * 0.02)] ?? 0;
        const hi = sample[Math.floor(sample.length * 0.98)] ?? 255;
        return [lo, 255 / Math.max(hi - lo, 1e-6)];
    });

    const canvas = document.createElement("canvas");
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext("2d");
    const out = ctx.createImageData(width, height);
    const px = out.data;
    for (let i = 0; i < width * height; i++) {
        for (let c = 0; c < 3; c++) {
            const b = rasters.length >= 3 ? c : 0;
            const [lo, mul] = scale[b];
            px[i * 4 + c] = (rasters[b][i] - lo) * mul;   // Uint8ClampedArray clamps
        }
        px[i * 4 + 3] = 255;
    }
    ctx.putImageData(out, 0, 0);
    const blob = await new Promise((res, rej) => canvas.toBlob((b) => (b ? res(b) : rej(new Error("PNG encode failed"))), "image/png"));
    return new File([blob], file.name.replace(/\.tiff?$/i, ".png"), { type: "image/png" });
}


export default function PotholePanel() {
    const [items, setItems] = useState([]);          // { id, file, inputUrl, status, resultUrl, message, count, ms }
    const [mode, setMode] = useState(MODES[0]);
    const [conf, setConf] = useState(0.15);
    const [roadsOnly, setRoadsOnly] = useState(true);
    const [running, setRunning] = useState(false);
    const [stage, setStage] = useState("");
    const [drag, setDrag] = useState(false);
    const [error, setError] = useState(null);
    const stopRef = useRef(false);
    const inputRef = useRef(null);

    // Warm the Space while the user is still choosing files.
    useEffect(() => { warm(); }, []);
    useEffect(() => () => items.forEach((i) => i.inputUrl && URL.revokeObjectURL(i.inputUrl)), []); // eslint-disable-line react-hooks/exhaustive-deps

    const addFiles = (list) => {
        setError(null);
        const ok = [], skipped = [];
        for (const f of list) {
            if (!ACCEPT.test(f.name)) skipped.push(`${f.name} (not an image)`);
            else if (f.size > MAX_MB * 1024 * 1024) skipped.push(`${f.name} (over ${MAX_MB} MB)`);
            else {
                const isTif = /\.tiff?$/i.test(f.name);
                ok.push({
                    cache: f.cache || null,              // tour samples: a stored result if the model cannot be reached
                    id: `${f.name}-${f.size}-${Math.random().toString(36).slice(2, 7)}`,
                    file: f, upload: isTif ? null : f,
                    inputUrl: isTif ? null : URL.createObjectURL(f),
                    status: isTif ? "preparing" : "queued",
                });
            }
        }
        if (skipped.length) setError(`Skipped: ${skipped.join(", ")}`);
        setItems((prev) => [...prev, ...ok]);
        // Decode TIFFs after they appear in the list, so the UI never waits on them.
        for (const it of ok.filter((i) => i.status === "preparing")) {
            isRealTiff(it.file).then((tiff) => (tiff ? tiffToPng(it.file) : asImageFile(it.file)))
                .then((png) => patch(it.id, { upload: png, inputUrl: URL.createObjectURL(png), status: "queued" }))
                .catch((e) => patch(it.id, { status: "error", message: `Could not read this image: ${String(e?.message || e).slice(0, 120)}` }));
        }
    };

    const patch = (id, p) => setItems((prev) => prev.map((i) => (i.id === id ? { ...i, ...p } : i)));
    const remove = (id) => setItems((prev) => {
        const it = prev.find((i) => i.id === id);
        if (it?.inputUrl) URL.revokeObjectURL(it.inputUrl);
        return prev.filter((i) => i.id !== id);
    });

    const run = async () => {
        const queue = items.filter((i) => i.upload && (i.status === "queued" || i.status === "error"));
        if (!queue.length) return;
        setRunning(true);
        setError(null);
        stopRef.current = false;
        setStage("Waking the pothole model… (the first run after a quiet spell can take a minute)");
        await warm();
        for (let n = 0; n < queue.length; n++) {
            if (stopRef.current) break;
            const it = queue[n];
            setStage(`Detecting road damage · image ${n + 1} of ${queue.length}`);
            patch(it.id, { status: "running", message: "" });
            const t0 = performance.now();
            try {
                let d;
                try {
                    d = await detect(it.upload, mode, conf, roadsOnly);
                } catch (e) {
                    if (!it.cache) throw e;
                    d = await (await fetch(it.cache)).json();      // tour sample: stored result of the same model
                }
                const ms = Math.round(performance.now() - t0);
                const breakdown = Object.entries(d.counts || {}).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(" · ");
                const gate = d.road_filter === "on" ? ` · ${d.filtered_off_road || 0} off-road removed`
                    : d.road_filter === "unavailable" ? " · roads-only needs the GPU server" : "";
                const message = (d.count
                    ? `${breakdown} · confidence ≥ ${d.confidence_used}${d.tiled ? " · tiled" : ""}`
                    : `No road damage found · confidence ≥ ${d.confidence_used}`) + gate;
                patch(it.id, { status: "done", resultUrl: d.image, message, count: d.count, potholes: d.potholes, ms,
                    detections: d.detections || [], size: d.width ? `${d.width}×${d.height}` : null });
                logActivity({
                    type: "Pothole Detection", district: "Custom Upload", area: it.file.name, status: "Completed",
                    metrics: { "Road damage": d.count, "Potholes": d.potholes, ...(d.counts || {}) },
                    meta: { sizeKB: Math.round(it.file.size / 1024), seconds: +(ms / 1000).toFixed(1), mode: d.mode, conf: d.confidence_used },
                });
            } catch (e) {
                const raw = String(e?.message || e);
                const msg = /Failed to fetch|NetworkError/i.test(raw)
                    ? "Could not reach the pothole model. The Space may be waking up — try again in a minute."
                    : raw.slice(0, 200);
                patch(it.id, { status: "error", message: msg });
                logActivity({ type: "Pothole Detection", district: "Custom Upload", area: it.file.name, status: "Failed", meta: { error: msg } });
            }
        }
        setRunning(false);
        setStage("");
    };

    // Guided "AI for Roads" walkthrough drives this panel like a user would.
    const api = useRef({});
    api.current = { addFiles, run, setRoadsOnly, setMode, clear: () => setItems([]) };
    useEffect(() => {
        registerTour("pothole", {
            loadSamples: async (samples) => {
                const files = [];
                for (const s of samples) {
                    const f = await fetchFile(s.url, s.name, "image/jpeg");
                    f.cache = s.cache;
                    files.push(f);
                }
                api.current.clear();
                api.current.addFiles(files);
            },
            setRoadsOnly: (v) => api.current.setRoadsOnly(v),
            setMode: (m) => api.current.setMode(m),
            run: () => { api.current.run(); },
        });
        return () => unregisterTour("pothole");
    }, []);

    const queued = items.filter((i) => i.upload && (i.status === "queued" || i.status === "error")).length;
    const preparing = items.some((i) => i.status === "preparing");
    const done = items.filter((i) => i.status === "done");
    const totalFound = done.reduce((s, i) => s + (i.count || 0), 0);

    return (
        <div className="space-y-3" data-tour="pothole-panel">
            {/* Drop zone */}
            <div
                onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
                onDragLeave={() => setDrag(false)}
                onDrop={(e) => { e.preventDefault(); setDrag(false); addFiles([...e.dataTransfer.files]); }}
                onClick={() => inputRef.current?.click()}
                className="cursor-pointer rounded-[8px] border-2 border-dashed p-7 text-center transition-colors"
                style={{
                    background: drag ? "var(--signal-dim)" : "var(--surface)",
                    borderColor: drag ? "var(--signal)" : "var(--line)",
                }}
            >
                <ImagePlus size={26} className="mx-auto mb-2" style={{ color: "var(--signal)" }} />
                <p className="display text-[14px] font-semibold" style={{ color: "var(--text)" }}>Drop drone images of roads</p>
                <p className="mt-1 text-[12px]" style={{ color: "var(--text-dim)" }}>or click to browse · several at once · JPG, PNG, WEBP, TIFF up to {MAX_MB} MB</p>
                <input ref={inputRef} type="file" multiple accept="image/*,.tif,.tiff" className="hidden"
                    onChange={(e) => { addFiles([...e.target.files]); e.target.value = ""; }} />
            </div>

            {/* Settings + run */}
            <Card>
                <SectionHeader right={<Pill mono>YOLOv8s · 1024 px</Pill>}>Detection settings</SectionHeader>
                <div className="space-y-3 px-3 pb-3">
                    <div className="grid grid-cols-2 gap-1.5">
                        {MODES.map((m) => (
                            <button key={m} onClick={() => setMode(m)} disabled={running}
                                className="rounded-[6px] px-3 py-2 text-left transition-colors disabled:opacity-50"
                                style={{
                                    border: `1px solid ${mode === m ? "var(--signal)" : "var(--line)"}`,
                                    background: mode === m ? "var(--signal-dim)" : "transparent",
                                    boxShadow: mode === m ? "inset 2px 0 0 var(--signal)" : "none",
                                }}>
                                <div className="text-[12.5px] font-medium" style={{ color: "var(--text)" }}>{m === MODES[0] ? "Auto" : "Manual"}</div>
                                <div className="text-[10.5px]" style={{ color: "var(--text-mute)" }}>
                                    {m === MODES[0] ? "Model picks the confidence per image" : "You set the confidence threshold"}
                                </div>
                            </button>
                        ))}
                    </div>
                    <button onClick={() => setRoadsOnly((v) => !v)} disabled={running}
                        className="flex w-full items-center justify-between rounded-[6px] px-3 py-2 text-left transition-colors disabled:opacity-50"
                        style={{ border: `1px solid ${roadsOnly ? "var(--signal)" : "var(--line)"}`, background: roadsOnly ? "var(--signal-dim)" : "transparent" }}>
                        <span>
                            <span className="block text-[12.5px] font-medium" style={{ color: "var(--text)" }}>Roads only</span>
                            <span className="block text-[10.5px]" style={{ color: "var(--text-mute)" }}>Ignore detections on roofs, fields and open plots (GPU server)</span>
                        </span>
                        <span className="h-[14px] w-[24px] shrink-0 rounded-full p-[2px] transition-colors" style={{ background: roadsOnly ? "var(--signal)" : "var(--line)" }}>
                            <span className="block h-[10px] w-[10px] rounded-full bg-white transition-transform" style={{ transform: roadsOnly ? "translateX(10px)" : "none" }} />
                        </span>
                    </button>
                    {mode === "manual" && (
                        <label className="anim-fade-up block">
                            <span className="mb-1.5 flex items-center justify-between text-[11px]" style={{ color: "var(--text-dim)" }}>
                                <span>Confidence threshold</span><span className="mono" style={{ color: "var(--signal)" }}>{conf.toFixed(2)}</span>
                            </span>
                            <input type="range" min="0.1" max="1" step="0.05" value={conf} disabled={running}
                                onChange={(e) => setConf(parseFloat(e.target.value))} className="w-full cursor-pointer" />
                            <span className="mt-1 flex justify-between text-[10px]" style={{ color: "var(--text-mute)" }}>
                                <span>more detections</span><span>fewer, surer</span>
                            </span>
                        </label>
                    )}
                    <div className="flex gap-1.5">
                        <Button data-tour="pothole-run" variant="primary" className={`flex-1 !py-2 ${running ? "animate-pulse" : ""}`} onClick={run} disabled={running || !queued}>
                            {running ? <Spinner size={13} /> : <Play size={13} />}
                            {running ? "Detecting…" : queued ? `Detect road damage (${queued} image${queued > 1 ? "s" : ""})` : preparing ? "Preparing TIFF…" : items.length ? "All images analysed" : "Add images to start"}
                        </Button>
                        {running && (
                            <Button onClick={() => { stopRef.current = true; }} style={{ color: "var(--critical)" }} title="Stop after the current image">
                                <Square size={12} /> Stop
                            </Button>
                        )}
                        {!running && items.length > 0 && (
                            <Button onClick={() => { items.forEach((i) => i.inputUrl && URL.revokeObjectURL(i.inputUrl)); setItems([]); }} title="Clear all">
                                <RotateCcw size={12} /> Clear
                            </Button>
                        )}
                    </div>
                    {stage && (
                        <div>
                            <p className="mono mb-1.5 text-[11px]" style={{ color: "var(--signal)" }}>{stage}</p>
                            <div className="progress-sweep rounded" />
                        </div>
                    )}
                    {error && <p className="text-[11px]" style={{ color: "var(--critical)" }}>{error}</p>}
                </div>
            </Card>

            {/* Summary */}
            {done.length > 0 && (
                <div className="stagger grid grid-cols-3 gap-2">
                    {[
                        ["Images analysed", done.length, "#2DD4BF"],
                        ["Road damage found", totalFound, totalFound ? "#F5A524" : "#64748B"],
                        ["Avg time / image", `${(done.reduce((s, i) => s + (i.ms || 0), 0) / done.length / 1000).toFixed(1)}s`, "#38BDF8"],
                    ].map(([k, v, c]) => (
                        <div key={k} className="stat-card" style={{ "--stat-colour": c }}>
                            <div className="min-w-0 flex-1">
                                <div className="stat-card-label">{k}</div>
                                <div className="stat-card-value">{v}</div>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {/* Results */}
            <div className="space-y-2">
                {items.map((it, idx) => (
                    <Card key={it.id} data-tour={`pothole-result-${idx}`} className="anim-fade-up overflow-hidden">
                        <div className="flex items-center gap-2 px-3 py-2" style={{ borderBottom: "1px solid var(--line)" }}>
                            <span className="mono min-w-0 flex-1 truncate text-[11.5px]" style={{ color: "var(--text)" }} title={it.file.name}>{it.file.name}</span>
                            {it.status === "preparing" && <Pill colour="var(--signal)"><Spinner size={9} /> Reading TIFF</Pill>}
                            {it.status === "queued" && <Pill>Queued</Pill>}
                            {it.status === "running" && <Pill colour="var(--signal)"><Spinner size={9} /> Detecting</Pill>}
                            {it.status === "done" && (
                                <Pill colour={it.count ? "var(--alert)" : "var(--ok)"}>
                                    {it.count != null ? `${it.count} issue${it.count === 1 ? "" : "s"}` : "Done"}
                                </Pill>
                            )}
                            {it.status === "error" && <Pill colour="var(--critical)">Failed</Pill>}
                            {it.status === "done" && it.resultUrl && (
                                <a href={it.resultUrl} target="_blank" rel="noreferrer" download
                                    className="rounded-[5px] p-1 hover:bg-[var(--surface-2)]" style={{ color: "var(--text-dim)" }} title="Open / download result">
                                    <Download size={13} />
                                </a>
                            )}
                            {!running && (
                                <button onClick={() => remove(it.id)} className="rounded-[5px] p-1 hover:bg-[var(--surface-2)]"
                                    style={{ color: "var(--text-mute)" }} aria-label="Remove"><X size={13} /></button>
                            )}
                        </div>
                        <div className="grid grid-cols-1 gap-px sm:grid-cols-2" style={{ background: "var(--line)" }}>
                            <figure className="relative" style={{ background: "var(--ink)" }}>
                                {it.inputUrl ? (
                                    <img src={it.inputUrl} alt={`Input ${it.file.name}`} className="anim-fade h-[240px] w-full object-contain" />
                                ) : (
                                    <div className="flex h-[240px] items-center justify-center gap-2 text-[11px]" style={{ color: "var(--text-dim)" }}>
                                        {it.status === "preparing" ? <><Spinner size={13} /> Converting TIFF for preview…</> : "No preview"}
                                    </div>
                                )}
                                <figcaption className="panel-title absolute left-2 top-2 rounded-[4px] px-1.5 py-0.5" style={{ background: "rgba(11,18,32,.8)", fontSize: 9.5 }}>Input</figcaption>
                            </figure>
                            <figure className="relative flex items-center justify-center" style={{ background: "var(--ink)", minHeight: 240 }}>
                                {it.status === "done" && it.resultUrl ? (
                                    <img src={it.resultUrl} alt="Pothole detection output" className="anim-fade h-[240px] w-full object-contain" />
                                ) : it.status === "running" ? (
                                    <div className="w-2/3 text-center">
                                        <Spinner size={18} />
                                        <p className="mt-2 text-[11px]" style={{ color: "var(--text-dim)" }}>Running YOLO on this image…</p>
                                        <div className="progress-sweep mt-3 rounded" />
                                    </div>
                                ) : it.status === "error" ? (
                                    <p className="px-4 text-center text-[11px]" style={{ color: "var(--critical)" }}>{it.message}</p>
                                ) : (
                                    <p className="text-[11px]" style={{ color: "var(--text-mute)" }}>Result appears here</p>
                                )}
                                <figcaption className="panel-title absolute left-2 top-2 rounded-[4px] px-1.5 py-0.5" style={{ background: "rgba(11,18,32,.8)", fontSize: 9.5 }}>Detections</figcaption>
                            </figure>
                        </div>
                        {it.status === "done" && it.message && (
                            <p className="mono px-3 py-1.5 text-[10.5px]" style={{ color: "var(--text-dim)", borderTop: "1px solid var(--line)" }}>
                                {it.message} · {(it.ms / 1000).toFixed(1)}s
                            </p>
                        )}
                        {it.status === "done" && it.detections?.length > 0 && (
                            <div className="stagger flex flex-wrap gap-1.5 px-3 pb-2.5 pt-0.5" data-tour="pothole-detections">
                                {it.detections.map((d, k) => (
                                    <span key={k} className="anim-fade-up inline-flex items-center gap-1.5 rounded-[5px] px-2 py-1 text-[10.5px]"
                                        style={{ border: "1px solid var(--line)", background: "var(--surface-2)", color: "var(--text)" }}>
                                        <span className="h-2 w-2 rounded-full" style={{ background: d.is_pothole ? "#EF4444" : "#F5A524", boxShadow: `0 0 8px ${d.is_pothole ? "#EF4444" : "#F5A524"}` }} />
                                        {d.label}
                                        <span className="mono" style={{ color: "var(--signal)" }}>{Math.round(d.confidence * 100)}%</span>
                                        <span className="mono" style={{ color: "var(--text-mute)" }}>{Math.round(d.bbox_xyxy[2] - d.bbox_xyxy[0])}×{Math.round(d.bbox_xyxy[3] - d.bbox_xyxy[1])} px</span>
                                    </span>
                                ))}
                            </div>
                        )}
                    </Card>
                ))}
            </div>
        </div>
    );
}
