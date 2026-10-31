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
 * There are deliberately no settings. The two that used to be here — ground
 * resolution and quality — were the two most dangerous things to get wrong,
 * and asking the operator to supply them meant a mis-set dropdown silently
 * halved the accuracy. The server now reads the resolution from a GeoTIFF's
 * own transform, or finds it by probing when the file does not carry one, and
 * always runs the four-resolution agreement pass. Quality is not a choice
 * worth offering when one option is simply better.
 *
 * GPU-only on purpose: it is a 1.2 GB model run at four resolutions, which
 * the CPU Hugging Face Space cannot carry. Falling back would 404 and look
 * like a bug, so the panel says plainly that the server is offline instead.
 */
const ACCEPT = /\.(jpe?g|png|webp|bmp|tiff?|jp2)$/i;
const MAX_MB = 200;

async function delineate(file, signal) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    // No gsd: the server reads it from the file, or works it out.
    const r = await gpuOnlyFetch("/hisup/predict", { method: "POST", body: fd, signal });
    if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
        if (r.status === 404) detail = "This GPU server has no building model installed.";
        if (r.status === 503) detail = "The building model isn't installed on the server yet.";
        throw new Error(detail);
    }
    return r.json();
}

function drawOverlay(canvas, img, geojson) {
    if (!canvas || !img?.naturalWidth) return;
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

function saveGeoJSON(geojson, name) {
    const blob = new Blob([JSON.stringify(geojson, null, 1)], { type: "application/geo+json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name.replace(/\.[^.]+$/, "")}_footprints.geojson`;
    a.click();
    URL.revokeObjectURL(a.href);
}

// How the server decided the ground resolution, in the operator's words.
const SOURCE_NOTE = {
    georeferenced: "read from the file",
    auto: "detected automatically",
    declared: "as supplied",
};

function ResultCard({ item }) {
    const imgRef = useRef(null);
    const canvasRef = useRef(null);
    const url = useRef(URL.createObjectURL(item.file)).current;

    useEffect(() => () => URL.revokeObjectURL(url), [url]);
    useEffect(() => {
        if (item.res && imgRef.current?.complete) drawOverlay(canvasRef.current, imgRef.current, item.res.geojson);
    }, [item.res]);

    const { res, err, file } = item;

    return (
        <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
                <span className="mono truncate text-[11px] text-[var(--text-primary)]">{file.name}</span>
                {res && (
                    <Button variant="ghost" onClick={() => saveGeoJSON(res.geojson, file.name)}>
                        <Download className="h-3.5 w-3.5" /> GeoJSON
                    </Button>
                )}
            </div>

            {item.state === "running" && (
                <div className="flex items-center gap-2 px-1 py-3 text-[11px] text-[var(--text-muted)]">
                    <Spinner className="h-3.5 w-3.5" /> Delineating…
                </div>
            )}

            {err && (
                <p className="rounded-md border border-[var(--danger)] bg-[var(--danger-dim)] px-3 py-2 text-[11px] text-[var(--danger)]">
                    {err}
                </p>
            )}

            {res && (
                <>
                    <div className="mb-2 grid grid-cols-3 gap-2">
                        {[["Buildings", res.count],
                          ["Seconds", res.seconds],
                          ["Resolution", `${(res.image.gsd * 100).toFixed(1)} cm`]].map(([k, v]) => (
                            <div key={k} className="rounded-md border border-[var(--line)] bg-[var(--surface)] px-2.5 py-1.5">
                                <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">{k}</div>
                                <div className="mono text-sm text-[var(--text-primary)]">{v}</div>
                            </div>
                        ))}
                    </div>
                    <p className="mb-2 text-[10px] text-[var(--text-muted)]">
                        Ground resolution {SOURCE_NOTE[res.image.gsd_source] || "as supplied"}.
                    </p>
                    <div className="overflow-hidden rounded-md border border-[var(--line)]">
                        <canvas ref={canvasRef} className="block w-full" />
                    </div>
                </>
            )}

            <img ref={imgRef} src={url} alt="" className="hidden"
                 onLoad={() => item.res && drawOverlay(canvasRef.current, imgRef.current, item.res.geojson)} />
        </div>
    );
}

export default function BuildingsPanel() {
    const [items, setItems] = useState([]);     // { file, state, res, err }
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const abortRef = useRef(null);
    const gpu = useGpuStatus();

    useEffect(() => { probeGpu(true); }, []);

    const pick = (fileList) => {
        setErr("");
        const incoming = Array.from(fileList || []);
        if (!incoming.length) return;
        const good = [];
        const bad = [];
        for (const f of incoming) {
            if (!ACCEPT.test(f.name)) bad.push(`${f.name} — unsupported format`);
            else if (f.size > MAX_MB * 1024 * 1024) bad.push(`${f.name} — ${(f.size / 1048576).toFixed(0)} MB, over the ${MAX_MB} MB limit`);
            else good.push({ file: f, state: "queued", res: null, err: "" });
        }
        if (bad.length) setErr(bad.join(" · "));
        if (good.length) setItems((prev) => [...prev, ...good]);
    };

    const run = async () => {
        setBusy(true);
        setErr("");
        abortRef.current = new AbortController();
        const signal = abortRef.current.signal;
        let done = 0;

        // One at a time: the model is 1.2 GB on a 6 GB card, and two concurrent
        // four-scale passes would simply run it out of memory.
        for (let i = 0; i < items.length; i++) {
            if (signal.aborted) break;
            if (items[i].res) { done += 1; continue; }
            setItems((prev) => prev.map((it, j) => (j === i ? { ...it, state: "running", err: "" } : it)));
            try {
                const out = await delineate(items[i].file, signal);
                setItems((prev) => prev.map((it, j) => (j === i ? { ...it, state: "done", res: out } : it)));
                done += 1;
            } catch (e) {
                if (e.name === "AbortError") break;
                const msg = e.message === "GPU_OFFLINE"
                    ? "The GPU server is offline. Start it with start_everything.bat."
                    : e.message;
                setItems((prev) => prev.map((it, j) => (j === i ? { ...it, state: "error", err: msg } : it)));
            }
        }

        setBusy(false);
        if (done) logActivity?.({ kind: "model", label: `Building delineation — ${done} image(s)` });
    };

    const downloadAll = () => {
        const feats = [];
        for (const it of items) {
            for (const f of it.res?.geojson?.features || []) {
                feats.push({ ...f, properties: { ...f.properties, source_image: it.file.name } });
            }
        }
        saveGeoJSON({ type: "FeatureCollection", features: feats }, "all_images");
    };

    const total = items.reduce((n, it) => n + (it.res?.count || 0), 0);
    const pending = items.some((it) => !it.res);
    const anyResult = items.some((it) => it.res);

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

            <div
                onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files); }}
                onDragOver={(e) => e.preventDefault()}
                onClick={() => document.getElementById("bld-input").click()}
                className="mt-3 cursor-pointer rounded-lg border-2 border-dashed border-[var(--border-default)] p-6 text-center transition-colors hover:border-[var(--accent)] hover:bg-[var(--accent-dim)]"
            >
                <ImagePlus className="mx-auto mb-2 h-7 w-7 text-[var(--text-muted)]" />
                <p className="text-xs font-medium text-[var(--text-primary)]">
                    Drop orthophotos here, or click to choose
                </p>
                <p className="mt-1 text-[10px] text-[var(--text-muted)]">
                    Several at once · JPG · PNG · TIFF · WebP · BMP · JP2 · up to {MAX_MB} MB each
                </p>
                <p className="mt-1 text-[10px] text-[var(--text-muted)]">
                    Nothing to configure — the ground resolution is read from the file, or worked out.
                </p>
                <input id="bld-input" type="file" multiple className="hidden"
                       accept="image/*,.tif,.tiff,.jp2"
                       onChange={(e) => pick(e.target.files)} />
            </div>

            {err && (
                <p className="mt-3 rounded-md border border-[var(--danger)] bg-[var(--danger-dim)] px-3 py-2 text-[11px] text-[var(--danger)]">
                    {err}
                </p>
            )}

            {items.length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-2">
                    <Button onClick={run} disabled={busy || !gpu.ok || !pending}>
                        {busy ? <Spinner className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                        {busy ? "Delineating…" : `Delineate ${items.length} image${items.length > 1 ? "s" : ""}`}
                    </Button>
                    {busy && (
                        <Button variant="ghost" onClick={() => abortRef.current?.abort()}>
                            <RotateCcw className="h-3.5 w-3.5" /> Cancel
                        </Button>
                    )}
                    {anyResult && !busy && (
                        <Button variant="ghost" onClick={downloadAll}>
                            <Download className="h-3.5 w-3.5" /> All as one GeoJSON
                        </Button>
                    )}
                    {!busy && (
                        <Button variant="ghost" onClick={() => { setItems([]); setErr(""); }}>
                            <X className="h-3.5 w-3.5" /> Clear
                        </Button>
                    )}
                    {total > 0 && (
                        <span className="mono text-[11px] text-[var(--text-muted)]">
                            {total.toLocaleString()} buildings across {items.filter((i) => i.res).length} image(s)
                        </span>
                    )}
                </div>
            )}

            {items.length > 0 && (
                <div className="mt-3 space-y-3">
                    {items.map((it, i) => <ResultCard key={`${it.file.name}-${i}`} item={it} />)}
                </div>
            )}
        </Card>
    );
}
