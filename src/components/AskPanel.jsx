import { useCallback, useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import { MessageSquare, Send, X, Play, Square, Sparkles, MapPin } from "lucide-react";
import { gpuBase, probeGpu, useGpuStatus } from "../lib/modelApi";

/*
 * "Ask the survey" — a question in English, answered from the road knowledge
 * base on the server (Qwen2.5-VL via Ollama turns the sentence into a query
 * plan; every number comes from the data, not the model).
 *
 * Answers land on the map: matched roads are pinned and highlighted, and
 * "Drive it" flies a camera along the road centreline like a survey vehicle.
 */
const SRC = "ask-roads";
const PIN = "ask-pins";
const CAR = "ask-car";
const IMG = "ask-img";

const SUGGESTIONS = [
    "Give me the most damaged road",
    "How many buildings are there?",
    "What is the largest water body?",
    "Total road length in Ongole",
    "Compare 2017 and 2026 buildings",
    "What data do you have?",
];

const GRADE_COLOUR = {
    critical: "#FF1744", bad: "#FF6D00", poor: "#FFC400",
    fair: "#64DD17", good: "#00E5FF",
};

export default function AskPanel({ getMap }) {
    const gpu = useGpuStatus();
    const [open, setOpen] = useState(false);
    const [q, setQ] = useState("");
    const [busy, setBusy] = useState(false);
    const [turns, setTurns] = useState([]);
    const [driving, setDriving] = useState(null);
    const drive = useRef({ timer: 0, stop: false, go: null });
    const [step, setStep] = useState(null);
    const endRef = useRef(null);

    useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [turns, busy]);

    const clearMap = useCallback(() => {
        const map = getMap?.();
        if (!map) return;
        for (const id of [`${SRC}-line`, `${SRC}-glow`, `${PIN}-dot`, `${PIN}-label`, CAR])
            if (map.getLayer(id)) map.removeLayer(id);
        for (const id of [SRC, PIN, CAR]) if (map.getSource(id)) map.removeSource(id);
    }, [getMap]);

    /* Draw the answer: the road itself, a numbered pin per road, and fit to it. */
    /* Buildings, water bodies and road segments returned for non-damage
     * questions: outline each one and fit to them. */
    const showFeatures = useCallback((feats) => {
        const map = getMap?.();
        if (!map || !feats?.length) return;
        clearMap();
        const fc = { type: "FeatureCollection", features: [] };
        const pins = { type: "FeatureCollection", features: [] };
        let minX = 180, minY = 90, maxX = -180, maxY = -90;
        feats.forEach((f, i) => {
            for (const ring of f.parts || []) {
                if (!(ring?.length > 1)) continue;
                const line = f.kind && f.kind.includes("Line");
                fc.features.push({
                    type: "Feature", properties: {},
                    geometry: line ? { type: "LineString", coordinates: ring }
                                   : { type: "Polygon", coordinates: [ring] },
                });
                for (const [x, y] of ring) {
                    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
                    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
                }
            }
            pins.features.push({ type: "Feature",
                properties: { n: String(i + 1), v: `${Math.round(f.value).toLocaleString()} m²` },
                geometry: { type: "Point", coordinates: [f.lon, f.lat] } });
        });
        map.addSource(SRC, { type: "geojson", data: fc });
        map.addLayer({ id: `${SRC}-glow`, type: "line", source: SRC,
            paint: { "line-color": "#22D3EE", "line-width": 10, "line-opacity": 0.22, "line-blur": 6 } });
        map.addLayer({ id: `${SRC}-line`, type: "line", source: SRC,
            paint: { "line-color": "#22D3EE", "line-width": 2.5 } });
        map.addSource(PIN, { type: "geojson", data: pins });
        map.addLayer({ id: `${PIN}-dot`, type: "circle", source: PIN,
            paint: { "circle-radius": 13, "circle-color": "#22D3EE",
                     "circle-stroke-color": "#0B1220", "circle-stroke-width": 2 } });
        map.addLayer({ id: `${PIN}-label`, type: "symbol", source: PIN,
            layout: { "text-field": ["get", "n"], "text-size": 13,
                      "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
                      "text-allow-overlap": true },
            paint: { "text-color": "#0B1220" } });
        if (maxX > minX || maxY > minY)
            map.fitBounds([[minX, minY], [maxX, maxY]],
                { padding: { top: 90, left: 60, right: 480, bottom: 140 }, duration: 2000, maxZoom: 19 });
    }, [getMap, clearMap]);

    const show = useCallback((roads) => {
        const map = getMap?.();
        if (!map || !roads?.length) return;
        clearMap();
        const lines = { type: "FeatureCollection", features: [] };
        const pins = { type: "FeatureCollection", features: [] };
        let minX = 180, minY = 90, maxX = -180, maxY = -90;
        roads.forEach((r, i) => {
            const colour = GRADE_COLOUR[r.grade] || "#FF1744";
            // parts, not one line: the road is cut at junctions and joining the
            // pieces to draw them overstated some roads by up to 75 %
            for (const part of r.parts || []) {
                if (!(part?.length > 1)) continue;
                lines.features.push({
                    type: "Feature", properties: { colour },
                    geometry: { type: "LineString", coordinates: part },
                });
                for (const [x, y] of part) {
                    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
                    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
                }
            }
            pins.features.push({
                type: "Feature",
                properties: { colour, n: String(i + 1), road: r.road, grade: r.grade,
                              potholes: r.potholes },
                geometry: { type: "Point", coordinates: [r.lon, r.lat] },
            });
            minX = Math.min(minX, r.lon); maxX = Math.max(maxX, r.lon);
            minY = Math.min(minY, r.lat); maxY = Math.max(maxY, r.lat);
        });

        map.addSource(SRC, { type: "geojson", data: lines });
        map.addLayer({ id: `${SRC}-glow`, type: "line", source: SRC,
            layout: { "line-cap": "round", "line-join": "round" },
            paint: { "line-color": ["get", "colour"], "line-width": 14,
                     "line-opacity": 0.25, "line-blur": 6 } });
        map.addLayer({ id: `${SRC}-line`, type: "line", source: SRC,
            layout: { "line-cap": "round", "line-join": "round" },
            paint: { "line-color": ["get", "colour"], "line-width": 4, "line-opacity": 0.95 } });

        map.addSource(PIN, { type: "geojson", data: pins });
        map.addLayer({ id: `${PIN}-dot`, type: "circle", source: PIN,
            paint: { "circle-radius": 13, "circle-color": ["get", "colour"],
                     "circle-stroke-color": "#0B1220", "circle-stroke-width": 2,
                     "circle-opacity": 0.95 } });
        map.addLayer({ id: `${PIN}-label`, type: "symbol", source: PIN,
            layout: { "text-field": ["get", "n"], "text-size": 13,
                      "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
                      "text-allow-overlap": true },
            paint: { "text-color": "#0B1220" } });

        const click = (e) => {
            const p = e.features?.[0]?.properties || {};
            new mapboxgl.Popup({ maxWidth: "240px" }).setLngLat(e.lngLat)
                .setHTML(`<div class="popup-title">${p.road}</div>
                    <div class="popup-row"><span class="popup-key">Condition</span><span class="popup-value">${p.grade}</span></div>
                    <div class="popup-row"><span class="popup-key">Potholes</span><span class="popup-value">${p.potholes}</span></div>`)
                .addTo(map);
        };
        map.on("click", `${PIN}-dot`, click);

        if (maxX > minX || maxY > minY)
            map.fitBounds([[minX, minY], [maxX, maxY]],
                { padding: { top: 90, left: 60, right: 480, bottom: 140 }, duration: 2000, maxZoom: 19 });
    }, [getMap, clearMap]);

    /* Story mode: run a marker along the centreline with the camera following. */
    const stopTour = useCallback(() => {
        clearTimeout(drive.current.timer);
        drive.current.stop = true;
        const map = getMap?.();
        if (map) {
            if (map.getLayer(CAR)) map.removeLayer(CAR);
            if (map.getSource(CAR)) map.removeSource(CAR);
            if (map.getLayer(IMG)) map.removeLayer(IMG);
            if (map.getSource(IMG)) map.removeSource(IMG);
        }
        setDriving(null);
        setStep(null);
    }, [getMap]);

    /* Story mode, the way the walkthrough does it: stop at each pothole in turn,
     * from one end of the road to the other, with the damage layer switched on
     * so the box being discussed is visible. Flat view — no tilt. */
    const tourRoad = useCallback(async (r, startAt = 0) => {
        const map = getMap?.();
        const list = r.pothole_list || [];
        if (!map || !list.length) return;

        // Put the drone imagery under it. At z20 the basemap is a blur, and a
        // story about potholes has to show the pothole.
        if (!map.getSource(IMG)) {
            try {
                const imgs = await (await fetch(`${gpuBase()}/imagery`)).json();
                const hit = imgs.find((im) => {
                    const b = im.bounds;
                    return im.status === "ready" && im.kind !== "satellite" && Array.isArray(b)
                        && r.lon >= b[0] && r.lon <= b[2] && r.lat >= b[1] && r.lat <= b[3];
                });
                if (hit) {
                    map.addSource(IMG, { type: "raster", tileSize: 512, maxzoom: 21,
                        bounds: hit.bounds,
                        tiles: [`${gpuBase()}/imagery/${hit.id}/tiles/{z}/{x}/{y}.webp?size=512`] });
                    const under = map.getLayer(`${SRC}-glow`) ? `${SRC}-glow` : undefined;
                    map.addLayer({ id: IMG, type: "raster", source: IMG,
                        paint: { "raster-opacity": 1 } }, under);
                }
            } catch { /* basemap will have to do */ }
        }
        clearTimeout(drive.current.timer);
        drive.current.stop = false;
        setDriving(r.road);

        // the potholes themselves, so the story has something to point at
        if (!map.getSource(CAR)) {
            map.addSource(CAR, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
            map.addLayer({ id: CAR, type: "circle", source: CAR,
                paint: { "circle-radius": ["case", ["==", ["get", "on"], 1], 15, 7],
                         "circle-color": "#FF1744",
                         "circle-opacity": ["case", ["==", ["get", "on"], 1], 0.35, 0.18],
                         "circle-stroke-color": "#FF1744",
                         "circle-stroke-width": ["case", ["==", ["get", "on"], 1], 3, 1.5] } });
        }

        const goto = (i) => {
            if (drive.current.stop || i >= list.length) {
                if (!drive.current.stop) setDriving(null), setStep(null);
                return;
            }
            const p = list[i];
            setStep({ i: i + 1, n: list.length, p, road: r.road });
            map.getSource(CAR)?.setData({
                type: "FeatureCollection",
                features: list.map((x, k) => ({
                    type: "Feature", properties: { on: k === i ? 1 : 0 },
                    geometry: { type: "Point", coordinates: [x.lon, x.lat] },
                })),
            });
            map.easeTo({ center: [p.lon, p.lat], zoom: 20.2, pitch: 0, bearing: 0, duration: 1500 });
            drive.current.timer = setTimeout(() => goto(i + 1), 3600);
        };
        drive.current.go = goto;
        goto(startAt);
    }, [getMap]);

    useEffect(() => () => { drive.current.stop = true; clearTimeout(drive.current.timer); }, []);

    const send = async (text) => {
        const question = (text ?? q).trim();
        if (!question || busy) return;
        setQ("");
        setTurns((t) => [...t, { role: "you", text: question }]);
        setBusy(true);
        try {
            if (!(await probeGpu())) throw new Error("The data server is offline.");
            const r = await fetch(`${gpuBase()}/ask?q=${encodeURIComponent(question)}`);
            const d = await r.json();
            setTurns((t) => [...t, { role: "ai", text: d.answer || "No answer.",
                                     roads: d.roads || [], plan: d.plan }]);
            if (d.roads?.length) show(d.roads);
            else if (d.features?.length) showFeatures(d.features);
        } catch (e) {
            setTurns((t) => [...t, { role: "ai", text: `Could not answer: ${e.message}`, roads: [] }]);
        } finally {
            setBusy(false);
        }
    };

    if (!gpu.base) return null;

    return (
        <>
            {/* story caption — which pothole, where along the road, how certain */}
            {step && (
                <div className="absolute bottom-5 left-5 z-30 w-[380px] max-w-[calc(100vw-2rem)]
                                rounded-[10px] px-3.5 py-3"
                    style={{ background: "var(--surface)", border: "1px solid var(--line)",
                             boxShadow: "var(--sh-lg)", color: "var(--text)" }}>
                    <div className="flex items-center gap-2">
                        <span className="rounded-[5px] px-1.5 py-0.5 text-[11px] font-semibold"
                            style={{ background: "#FF1744", color: "#fff" }}>
                            {step.i}/{step.n}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium">{step.road}</span>
                        <button onClick={() => { clearTimeout(drive.current.timer); drive.current.go?.(Math.max(0, step.i - 2)); }}
                            className="rounded px-1.5 py-0.5 text-[11px] hover:bg-[var(--surface-2)]">Prev</button>
                        <button onClick={() => { clearTimeout(drive.current.timer); drive.current.go?.(step.i); }}
                            className="rounded px-1.5 py-0.5 text-[11px] hover:bg-[var(--surface-2)]">Next</button>
                        <button onClick={stopTour}
                            className="rounded p-1 hover:bg-[var(--surface-2)]"><Square size={12} /></button>
                    </div>
                    <div className="mt-1.5 flex items-center gap-3 text-[11.5px] opacity-80">
                        <span>{step.p.label}</span>
                        <span>confidence {step.p.confidence.toFixed(2)}</span>
                        <span>{step.p.size_m.toFixed(1)} m</span>
                        <span>{Math.round(step.p.along_m)} m along</span>
                    </div>
                    <div className="mt-2 h-[3px] w-full rounded bg-[var(--surface-2)]">
                        <div className="h-full rounded" style={{ width: `${(step.i / step.n) * 100}%`, background: "#FF1744" }} />
                    </div>
                </div>
            )}

            {!open && (
                <button onClick={() => setOpen(true)} title="Ask the survey"
                    className="absolute bottom-5 right-5 z-20 flex items-center gap-2 rounded-full
                               bg-[var(--accent)] px-4 py-2.5 text-[13px] font-medium text-black shadow-lg
                               hover:brightness-110">
                    <MessageSquare size={15} /> Ask the survey
                </button>
            )}
            {open && (
                <div className="absolute bottom-5 right-5 z-20 flex w-[380px] max-w-[calc(100vw-2rem)]
                                flex-col overflow-hidden rounded-[10px]"
                    style={{ maxHeight: "min(620px, 78vh)", background: "var(--surface)",
                             border: "1px solid var(--line)", boxShadow: "var(--sh-lg)",
                             color: "var(--text)" }}>
                    <div className="flex items-center gap-2 px-3 py-2.5" style={{ borderBottom: "1px solid var(--line)" }}>
                        <Sparkles size={14} style={{ color: "var(--accent)" }} />
                        <div className="min-w-0 flex-1">
                            <div className="text-[12px] font-medium">Ask the survey</div>
                            <div className="text-[10px] opacity-60">Roads, buildings, water · answers come from the data</div>
                        </div>
                        {driving && (
                            <button onClick={stopTour} title="Stop"
                                className="rounded p-1 hover:bg-[var(--surface-2)]"><Square size={13} /></button>
                        )}
                        <button onClick={() => { setOpen(false); stopTour(); clearMap(); }}
                            className="rounded p-1 hover:bg-[var(--surface-2)]"><X size={14} /></button>
                    </div>

                    <div className="flex-1 space-y-2.5 overflow-y-auto px-3 py-3">
                        {!turns.length && (
                            <div className="space-y-1.5">
                                <div className="text-[11px] opacity-60">Try asking:</div>
                                {SUGGESTIONS.map((s) => (
                                    <button key={s} onClick={() => send(s)}
                                        className="block w-full rounded-[6px] px-2.5 py-1.5 text-left text-[11.5px]
                                                   hover:bg-[var(--surface-2)]"
                                        style={{ border: "1px solid var(--line)", color: "var(--text)" }}>
                                        {s}
                                    </button>
                                ))}
                            </div>
                        )}
                        {turns.map((t, i) => (
                            <div key={i} className={t.role === "you" ? "text-right" : ""}>
                                <div className={`inline-block max-w-[94%] whitespace-pre-wrap rounded-[8px] px-2.5 py-1.5
                                    text-[12px] leading-[1.5] ${t.role === "you"
                                        ? "bg-[var(--accent)] text-black"
                                        : ""}`}
                                    style={t.role === "you" ? undefined : {
                                        background: "var(--surface-2)",
                                        border: "1px solid var(--line)", color: "var(--text)" }}>
                                    {t.text}
                                </div>
                                {!!t.roads?.length && (
                                    <div className="mt-1.5 space-y-1">
                                        {t.roads.slice(0, 6).map((r, k) => (
                                            <div key={r.road + k}
                                                className="flex items-center gap-2 rounded-[6px] px-2 py-1 text-[11px]"
                                                style={{ border: "1px solid var(--line)", background: "var(--surface-2)" }}>
                                                <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                                                    style={{ background: GRADE_COLOUR[r.grade] || "#FF1744" }} />
                                                <span className="min-w-0 flex-1 truncate">{r.road}</span>
                                                <span className="shrink-0 opacity-60">{r.potholes}p · {Math.round(r.length_m)}m</span>
                                                <button title="Zoom to it" onClick={() => show([r])}
                                                    className="shrink-0 rounded p-0.5 hover:bg-[var(--surface-2)]">
                                                    <MapPin size={12} />
                                                </button>
                                                <button title="Play the potholes one by one" onClick={() => tourRoad(r)}
                                                    disabled={!(r.pothole_list?.length)}
                                                    className="shrink-0 rounded p-0.5 hover:bg-[var(--surface-2)] disabled:opacity-30">
                                                    <Play size={12} />
                                                </button>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        ))}
                        {busy && <div className="text-[11px] opacity-60">Thinking…</div>}
                        <div ref={endRef} />
                    </div>

                    <div className="flex items-center gap-1.5 p-2" style={{ borderTop: "1px solid var(--line)" }}>
                        <input value={q} onChange={(e) => setQ(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && send()}
                            placeholder="Which road is worst?"
                            className="min-w-0 flex-1 rounded-[6px] px-2.5 py-1.5 text-[12px] outline-none"
                            style={{ border: "1px solid var(--line)", background: "var(--surface-2)",
                                     color: "var(--text)" }} />
                        <button onClick={() => send()} disabled={busy || !q.trim()}
                            className="rounded-[6px] bg-[var(--accent)] p-1.5 text-black disabled:opacity-40">
                            <Send size={14} />
                        </button>
                    </div>
                </div>
            )}
        </>
    );
}
