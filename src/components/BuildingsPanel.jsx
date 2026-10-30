import { useEffect, useRef, useState } from "react";
import { Download, ImagePlus, Play, RotateCcw, X } from "lucide-react";
import { logActivity } from "../lib/activityLog";
import { gpuOnlyFetch, probeGpu, useGpuStatus } from "../lib/modelApi";
import { Button, Card, Pill, SectionHeader, Spinner } from "./ui";

/*
 * Building footprint delineation — HiSup, fine-tuned on our own Ongole drone
 * survey.
 *
 * This is a different job from AI Segmentation. SegFormer answers "which
 * pixels are building"; this answers "how many buildings, and where does each
 * one end". In dense low-rise fabric where houses share walls, a semantic
 * mask merges a whole terrace into one shape, and counting or valuing
 * individual properties from it is impossible.
 *
 * GPU-only on purpose: it is a 1.2 GB model run at four resolutions, which
 * the CPU Hugging Face Space cannot carry. Falling back would 404 and look
 * like a bug, so the panel says plainly that the server is offline instead.
 */
const ACCEPT = /\.(jpe?g|png|webp|bmp|tiff?)$/i;
const MAX_MB = 60;

// Metres per pixel of the uploaded image. Everything depends on this: the
// model reads buildings at a particular size in pixels, and the server
// resamples to reach it. A wrong GSD is the single fastest way to get a bad
// result, so it is asked for rather than guessed.
const GSD_PRESETS = [
    { v: 0.03, label: "3 cm — raw drone orthophoto" },
    { v: 0.05, label: "5 cm — drone" },
    { v: 0.10, label: "10 cm — drone, downsampled" },
    { v: 0.125, label: "12.5 cm — our working resolution" },
    { v: 0.30, label: "30 cm — satellite" },
];

async function delineate(file, gsd, quality, signal) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("gsd", String(gsd));
    // "fast" is one pass at the resolution that scores best on its own;
    // "best" runs four and keeps what two or more agree on, which is worth
    // about five points of F1 for roughly four times the time.
    fd.append("scales", quality === "fast" ? "0.125" : "0.08,0.10,0.125,0.15");
    fd.append("min_votes", quality === "fast" ? "1" : "2");
    const r = await gpuOnlyFetch("/hisup/predict", { method: "POST", body: fd, signal });
    if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
        if (r.status === 503) detail = "The building model isn't installed on the server yet.";
        throw new Error(detail);
    }
    return r.json();
}

function drawOverlay(canvas, img, geojson) {
    const ctx = canvas.getContext("2d");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    ctx.drawImage(img, 0, 0);
    ctx.lineWidth = Math.max(1.5, canvas.width / 900);
    ctx.strokeStyle = "#22c55e";
    ctx.fillStyle = "rgba(34,197,94,0.14)";
    for (const f of geojson?.features || []) {
        const ring = f.geometry?.coordinates?.[0];
        if (!ring?.length) continue;
        ctx.beginPath();
        ring.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
    }
}

export default function BuildingsPanel() {
    const [file, setFile] = useState(null);
    const [gsd, setGsd] = useState(0.125);
    const [quality, setQuality] = useState("best");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const [res, setRes] = useState(null);
    const imgRef = useRef(null);
    const canvasRef = useRef(null);
    const abortRef = useRef(null);
    const gpu = useGpuStatus();

    useEffect(() => { probeGpu(true); }, []);

    useEffect(() => {
        if (res && imgRef.current?.complete) drawOverlay(canvasRef.current, imgRef.current, res.geojson);
    }, [res]);

    const pick = (f) => {
        setErr(""); setRes(null);
        if (!f) return;
        if (!ACCEPT.test(f.name)) return setErr("Use a JPG, PNG, WebP, BMP or TIFF.");
        if (f.size > MAX_MB * 1024 * 1024) return setErr(`That file is ${(f.size / 1048576).toFixed(0)} MB; the limit is ${MAX_MB} MB.`);
        setFile(f);
    };

    const run = async () => {
        if (!file) return;
        setBusy(true); setErr(""); setRes(null);
        abortRef.current = new AbortController();
        try {
            const out = await delineate(file, gsd, quality, abortRef.current.signal);
            setRes(out);
            logActivity?.({ kind: "model", label: `Building delineation — ${out.count} footprints` });
        } catch (e) {
            if (e.name === "AbortError") return;
            setErr(e.message === "GPU_OFFLINE"
                ? "The GPU server is offline. Start it with start_everything.bat on the survey laptop."
                : e.message);
        } finally {
            setBusy(false);
        }
    };

    const download = () => {
        const blob = new Blob([JSON.stringify(res.geojson, null, 1)], { type: "application/geo+json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `${(file?.name || "buildings").replace(/\.[^.]+$/, "")}_footprints.geojson`;
        a.click();
        URL.revokeObjectURL(a.href);
    };

    const reset = () => { setFile(null); setRes(null); setErr(""); };

    return (
        <Card>
            <SectionHeader
                title="Building delineation"
                sub="Individual footprints, not a mask — HiSup trained on our Ongole survey"
                right={
                    <div className="flex items-center gap-1.5">
                        <Pill tone={gpu.ok ? "ok" : "warn"}>{gpu.ok ? "GPU online" : "GPU offline"}</Pill>
                        <Pill>F1 0.81 held-out</Pill>
                    </div>
                }
            />

            {!file ? (
                <div
                    onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files[0]); }}
                    onDragOver={(e) => e.preventDefault()}
                    onClick={() => document.getElementById("bld-input").click()}
                    className="mt-3 cursor-pointer rounded-lg border-2 border-dashed border-[var(--border-default)] p-8 text-center transition-colors hover:border-[var(--accent)] hover:bg-[var(--accent-dim)]"
                >
                    <ImagePlus className="mx-auto mb-2 h-7 w-7 text-[var(--text-muted)]" />
                    <p className="text-xs font-medium text-[var(--text-primary)]">Drop an orthophoto, or click to choose</p>
                    <p className="mt-1 text-[10px] text-[var(--text-muted)]">JPG · PNG · TIFF · up to {MAX_MB} MB</p>
                    <input id="bld-input" type="file" className="hidden" accept="image/*,.tif,.tiff"
                           onChange={(e) => pick(e.target.files[0])} />
                </div>
            ) : (
                <div className="mt-3 space-y-3">
                    <div className="flex items-center justify-between rounded-md border border-[var(--line)] bg-[var(--surface-2)] px-3 py-2">
                        <span className="mono truncate text-[11px] text-[var(--text-primary)]">{file.name}</span>
                        <button onClick={reset} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
                            <X className="h-3.5 w-3.5" />
                        </button>
                    </div>

                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <label className="block">
                            <span className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-[var(--text-muted)]">
                                Image resolution
                            </span>
                            <select value={gsd} onChange={(e) => setGsd(Number(e.target.value))}
                                    className="w-full rounded-md border border-[var(--line)] bg-[var(--surface)] px-2 py-1.5 text-[11px]">
                                {GSD_PRESETS.map((p) => <option key={p.v} value={p.v}>{p.label}</option>)}
                            </select>
                            <span className="mt-1 block text-[10px] text-[var(--text-muted)]">
                                Metres per pixel. Getting this wrong costs more accuracy than any other setting.
                            </span>
                        </label>

                        <label className="block">
                            <span className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-[var(--text-muted)]">
                                Quality
                            </span>
                            <select value={quality} onChange={(e) => setQuality(e.target.value)}
                                    className="w-full rounded-md border border-[var(--line)] bg-[var(--surface)] px-2 py-1.5 text-[11px]">
                                <option value="best">Best — four resolutions, agreement filter</option>
                                <option value="fast">Fast — single pass</option>
                            </select>
                            <span className="mt-1 block text-[10px] text-[var(--text-muted)]">
                                Best is ~4× slower and finds about 5% more buildings correctly.
                            </span>
                        </label>
                    </div>

                    <div className="flex items-center gap-2">
                        <Button onClick={run} disabled={busy || !gpu.ok}>
                            {busy ? <Spinner className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                            {busy ? "Delineating…" : "Delineate buildings"}
                        </Button>
                        {busy && (
                            <Button variant="ghost" onClick={() => abortRef.current?.abort()}>
                                <RotateCcw className="h-3.5 w-3.5" /> Cancel
                            </Button>
                        )}
                        {res && (
                            <Button variant="ghost" onClick={download}>
                                <Download className="h-3.5 w-3.5" /> GeoJSON
                            </Button>
                        )}
                    </div>
                </div>
            )}

            {err && (
                <p className="mt-3 rounded-md border border-[var(--danger)] bg-[var(--danger-dim)] px-3 py-2 text-[11px] text-[var(--danger)]">
                    {err}
                </p>
            )}

            {res && (
                <div className="mt-4 space-y-3">
                    <div className="grid grid-cols-3 gap-2">
                        {[["Buildings", res.count],
                          ["Seconds", res.seconds],
                          ["Resolution", `${(res.image.gsd * 100).toFixed(1)} cm`]].map(([k, v]) => (
                            <div key={k} className="rounded-md border border-[var(--line)] bg-[var(--surface-2)] px-3 py-2">
                                <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">{k}</div>
                                <div className="mono text-sm text-[var(--text-primary)]">{v}</div>
                            </div>
                        ))}
                    </div>
                    <div className="overflow-hidden rounded-lg border border-[var(--line)]">
                        <canvas ref={canvasRef} className="block w-full" />
                    </div>
                    {file && (
                        <img ref={imgRef} src={URL.createObjectURL(file)} alt="" className="hidden"
                             onLoad={() => res && drawOverlay(canvasRef.current, imgRef.current, res.geojson)} />
                    )}
                </div>
            )}
        </Card>
    );
}
