import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Eye, EyeOff, ImagePlus, Play, RotateCcw, X } from "lucide-react";
import { logActivity } from "../lib/activityLog";
import { gpuOnlyFetch, useGpuStatus } from "../lib/modelApi";
import { Button, Card, Pill, SectionHeader, Spinner } from "./ui";

/*
 * Land-use / land-cover classification — DINOv3 ViT-L/16 + UPerNet, 9 classes.
 *
 * A different question from the other two models on this page. Building
 * Delineation answers "where does each building end"; AI Segmentation answers
 * "which of four things is this pixel". This answers "what is the ground
 * being used for", over the whole frame, in the nine OpenEarthMap classes —
 * which is the question a land-use survey asks before any of the others.
 *
 * The legend is fetched from the server rather than written here. The colours
 * were chosen by running every pair through the OKLab colour-difference checks
 * so that no two classes are confusable, and a second copy of them in this
 * file is a second copy that can drift. If the server changes a colour, the
 * key and the pixels change together.
 *
 * GPU-only, like the building model: a frozen ViT-L at four taps is not
 * something the CPU Hugging Face Space can carry, so falling back would 404
 * and read as a bug. The panel says the server is offline instead.
 */
const ACCEPT = /\.(jpe?g|png|webp|bmp|tiff?|jp2)$/i;
const MAX_MB = 200;

async function classify(file, signal) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("want_overlay", "true");
    // No gsd: a GeoTIFF carries its own, and the server refuses rather than
    // guesses for anything that does not.
    const r = await gpuOnlyFetch("/landcover/predict", { method: "POST", body: fd, signal });
    if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
        if (r.status === 404) detail = "This GPU server has no land-cover model installed.";
        if (r.status === 503) detail = "The land-cover model isn't installed on the server yet.";
        if (r.status === 400) detail += " — a plain JPG/PNG carries no ground resolution; upload the GeoTIFF.";
        throw new Error(detail);
    }
    return r.json();
}

const pct = (v) => `${(v ?? 0).toFixed(1)}%`;
const ha = (m2) => (m2 == null ? null : (m2 / 10000).toFixed(2));

function saveDataUrl(b64, mime, name) {
    const a = document.createElement("a");
    a.href = `data:${mime};base64,${b64}`;
    a.download = name;
    a.click();
}

function toCSV(rows, fileName) {
    const head = "class,percent,pixels,area_m2,area_ha,colour";
    const body = rows.map((r) => [r.name, r.percent, r.pixels ?? "", r.area_m2 ?? "",
                                  r.area_ha ?? "", r.color].join(","));
    const blob = new Blob([[head, ...body].join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = fileName;
    a.click();
    URL.revokeObjectURL(a.href);
}

/*
 * One bar per class, widths straight from the percentages.
 *
 * A pie was the obvious thing and is the wrong one: nine slices, several of
 * them under two per cent, and comparing angles is harder than comparing the
 * lengths of bars that share a baseline. Rows are sorted by share so the
 * dominant class is the first thing read, and each row carries its own name
 * and number, so identity never rests on colour alone.
 */
function ClassBars({ rows }) {
    const shown = rows.filter((r) => (r.percent ?? 0) >= 0.05);
    const max = Math.max(1, ...shown.map((r) => r.percent || 0));
    return (
        <div className="space-y-1.5">
            {shown.map((r) => (
                <div key={r.name} className="flex items-center gap-2">
                    <span className="h-3 w-3 shrink-0 rounded-sm"
                          style={{ background: r.color }} aria-hidden="true" />
                    <span className="w-24 shrink-0 text-[11px] text-[var(--text-primary)]">{r.label || r.name}</span>
                    <span className="h-2 min-w-[2px] rounded-sm"
                          style={{ background: r.color, width: `${(100 * (r.percent || 0)) / max}%` }} />
                    <span className="mono ml-auto shrink-0 text-[11px] text-[var(--text-secondary)]">
                        {pct(r.percent)}
                        {r.area_ha != null && (
                            <span className="ml-2 text-[var(--text-muted)]">{ha(r.area_m2)} ha</span>
                        )}
                    </span>
                </div>
            ))}
        </div>
    );
}

function ResultCard({ item }) {
    const [showOverlay, setShowOverlay] = useState(true);
    const { file, state, res, err } = item;
    const rows = (res?.classes || []).slice().sort((a, b) => (b.percent || 0) - (a.percent || 0));
    const src = res?.overlay_png ? `data:image/png;base64,${res.overlay_png}` : null;

    return (
        <div className="rounded-lg border border-[var(--border-default)] bg-[var(--surface-1)] p-3">
            <div className="flex flex-wrap items-center gap-2">
                <span className="truncate text-[11px] font-medium text-[var(--text-primary)]">{file.name}</span>
                {state === "running" && <Pill tone="warn">classifying…</Pill>}
                {state === "error" && <Pill tone="danger">failed</Pill>}
                {state === "done" && res && (
                    <>
                        <Pill tone="ok">{res.dominant}</Pill>
                        <span className="mono text-[10px] text-[var(--text-muted)]">
                            {res.image?.width}×{res.image?.height} · {res.image?.work_gsd?.toFixed?.(2)} m/px
                            {res.image?.resampled ? " (resampled)" : " (native)"} · {res.seconds}s
                        </span>
                    </>
                )}
                {state === "done" && res && (
                    <div className="ml-auto flex items-center gap-1.5">
                        <Button variant="ghost" onClick={() => setShowOverlay((v) => !v)}>
                            {showOverlay ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                            {showOverlay ? "Hide classes" : "Show classes"}
                        </Button>
                        <Button variant="ghost"
                                onClick={() => saveDataUrl(res.classes_png, "image/png",
                                                           `${file.name.replace(/\.[^.]+$/, "")}_classes.png`)}>
                            <Download className="h-3.5 w-3.5" /> Class map
                        </Button>
                        <Button variant="ghost"
                                onClick={() => toCSV(rows, `${file.name.replace(/\.[^.]+$/, "")}_landcover.csv`)}>
                            <Download className="h-3.5 w-3.5" /> CSV
                        </Button>
                    </div>
                )}
            </div>

            {err && (
                <p className="mt-2 text-[11px] text-[var(--danger)]">{err}</p>
            )}

            {src && (
                <div className="mt-2 overflow-hidden rounded-md border border-[var(--border-subtle)]">
                    {/* The original is kept underneath and the overlay toggled on top,
                        so the eye compares the same pixels in the same place rather
                        than two images side by side at half the size. */}
                    <img src={src} alt={`Land cover for ${file.name}`}
                         className="block w-full"
                         style={{ display: showOverlay ? "block" : "none" }} />
                    {!showOverlay && item.preview && (
                        <img src={item.preview} alt={file.name} className="block w-full" />
                    )}
                </div>
            )}

            {rows.length > 0 && (
                <div className="mt-3">
                    <ClassBars rows={rows} />
                </div>
            )}
        </div>
    );
}

export default function LandCoverPanel() {
    const gpu = useGpuStatus();
    const [items, setItems] = useState([]);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const [legend, setLegend] = useState([]);
    const abortRef = useRef(null);

    useEffect(() => () => abortRef.current?.abort(), []);

    // The key, from the server, so it cannot disagree with the pixels.
    useEffect(() => {
        let gone = false;
        gpuOnlyFetch("/landcover/legend")
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (!gone && d?.classes) setLegend(d.classes); })
            .catch(() => { /* the panel still works; it just shows no key yet */ });
        return () => { gone = true; };
    }, [gpu.ok]);

    const pick = useCallback((fileList) => {
        const incoming = Array.from(fileList || []);
        if (!incoming.length) return;
        const good = [], bad = [];
        for (const f of incoming) {
            if (!ACCEPT.test(f.name)) bad.push(`${f.name} — unsupported format`);
            else if (f.size > MAX_MB * 1024 * 1024) bad.push(`${f.name} — ${(f.size / 1048576).toFixed(0)} MB, over the ${MAX_MB} MB limit`);
            else good.push({
                file: f, state: "queued", res: null, err: "",
                preview: /\.(jpe?g|png|webp|bmp)$/i.test(f.name) ? URL.createObjectURL(f) : null,
            });
        }
        if (bad.length) setErr(bad.join(" · "));
        if (good.length) setItems((prev) => [...prev, ...good]);
    }, []);

    const run = async () => {
        setBusy(true); setErr("");
        abortRef.current = new AbortController();
        const signal = abortRef.current.signal;
        let done = 0;

        // One at a time: it is a 1.28 GB ViT-L on a 6 GB card, and two frames
        // at once would simply run it out of memory.
        for (let i = 0; i < items.length; i++) {
            if (signal.aborted) break;
            if (items[i].res) { done += 1; continue; }
            setItems((prev) => prev.map((it, j) => (j === i ? { ...it, state: "running", err: "" } : it)));
            try {
                const out = await classify(items[i].file, signal);
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
        if (done) logActivity?.({ kind: "model", label: `Land-cover classification — ${done} image(s)` });
    };

    const pending = items.some((it) => !it.res);

    return (
        <Card>
            <SectionHeader
                title="Land-use classification"
                sub="Nine land-cover classes over the whole frame — DINOv3 ViT-L/16 + UPerNet"
                right={
                    <div className="flex items-center gap-1.5">
                        <Pill tone={gpu.ok ? "ok" : "warn"}>{gpu.ok ? "GPU online" : "GPU offline"}</Pill>
                        <Pill>building IoU 0.77</Pill>
                    </div>
                }
            />

            {legend.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
                    {legend.filter((c) => c.name !== "unknown").map((c) => (
                        <span key={c.name} className="flex items-center gap-1.5" title={c.note}>
                            <span className="h-3 w-3 rounded-sm" style={{ background: c.color }} aria-hidden="true" />
                            <span className="text-[11px] text-[var(--text-secondary)]">{c.label || c.name}</span>
                        </span>
                    ))}
                </div>
            )}

            <div
                onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files); }}
                onDragOver={(e) => e.preventDefault()}
                onClick={() => document.getElementById("lc-input").click()}
                className="mt-3 cursor-pointer rounded-lg border-2 border-dashed border-[var(--border-default)] p-6 text-center transition-colors hover:border-[var(--accent)] hover:bg-[var(--accent-dim)]"
            >
                <ImagePlus className="mx-auto mb-2 h-7 w-7 text-[var(--text-muted)]" />
                <p className="text-xs font-medium text-[var(--text-primary)]">
                    Drop orthophotos here, or click to choose
                </p>
                <p className="mt-1 text-[10px] text-[var(--text-muted)]">
                    Several at once · GeoTIFF preferred · JPG · PNG · WebP · BMP · JP2 · up to {MAX_MB} MB each
                </p>
                <p className="mt-1 text-[10px] text-[var(--text-muted)]">
                    A GeoTIFF carries its ground resolution, so areas come back in hectares.
                </p>
                <input id="lc-input" type="file" multiple className="hidden"
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
                        {busy ? "Classifying…" : `Classify ${items.length} image${items.length > 1 ? "s" : ""}`}
                    </Button>
                    {busy && (
                        <Button variant="ghost" onClick={() => abortRef.current?.abort()}>
                            <RotateCcw className="h-3.5 w-3.5" /> Cancel
                        </Button>
                    )}
                    {!busy && (
                        <Button variant="ghost" onClick={() => { setItems([]); setErr(""); }}>
                            <X className="h-3.5 w-3.5" /> Clear
                        </Button>
                    )}
                </div>
            )}

            {items.length > 0 && (
                <div className="mt-3 space-y-3">
                    {items.map((it, i) => (
                        <ResultCard key={`${it.file.name}-${i}`} item={it} />
                    ))}
                </div>
            )}
        </Card>
    );
}
