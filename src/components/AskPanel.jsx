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

const SUGGESTIONS = [
    "Give me the most damaged road",
    "Top 5 worst roads",
    "How many potholes on Arundelpet 6th line?",
    "Which roads are in good condition?",
    "Summarise the survey",
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
    const drive = useRef({ raf: 0, stop: false });
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
    const show = useCallback((roads) => {
        const map = getMap?.();
        if (!map || !roads?.length) return;
        clearMap();
        const lines = { type: "FeatureCollection", features: [] };
        const pins = { type: "FeatureCollection", features: [] };
        let minX = 180, minY = 90, maxX = -180, maxY = -90;
        roads.forEach((r, i) => {
            const colour = GRADE_COLOUR[r.grade] || "#FF1744";
            if (r.path?.length > 1) {
                lines.features.push({
                    type: "Feature", properties: { colour },
                    geometry: { type: "LineString", coordinates: r.path },
                });
                for (const [x, y] of r.path) {
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
    const stopDrive = useCallback(() => {
        drive.current.stop = true;
        cancelAnimationFrame(drive.current.raf);
        const map = getMap?.();
        if (map && map.getLayer(CAR)) map.removeLayer(CAR);
        if (map && map.getSource(CAR)) map.removeSource(CAR);
        setDriving(null);
    }, [getMap]);

    const driveRoad = useCallback((r) => {
        const map = getMap?.();
        if (!map || !(r.path?.length > 1)) return;
        stopDrive();
        drive.current.stop = false;
        setDriving(r.road);

        // cumulative length so the marker moves at a constant speed, not a
        // constant number of vertices per frame
        const pts = r.path;
        const seg = [0];
        for (let i = 1; i < pts.length; i++) {
            const dx = (pts[i][0] - pts[i - 1][0]) * Math.cos(pts[i][1] * Math.PI / 180);
            const dy = pts[i][1] - pts[i - 1][1];
            seg.push(seg[i - 1] + Math.hypot(dx, dy));
        }
        const total = seg[seg.length - 1] || 1;
        const at = (t) => {
            const d = t * total;
            let i = seg.findIndex((s) => s >= d);
            if (i <= 0) i = 1;
            const f = (d - seg[i - 1]) / Math.max(seg[i] - seg[i - 1], 1e-12);
            return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f,
                    pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f];
        };
        const bearing = (a, b) => (Math.atan2(
            (b[0] - a[0]) * Math.cos(b[1] * Math.PI / 180), b[1] - a[1]) * 180) / Math.PI;

        map.addSource(CAR, { type: "geojson",
            data: { type: "Feature", geometry: { type: "Point", coordinates: pts[0] } } });
        map.addLayer({ id: CAR, type: "circle", source: CAR,
            paint: { "circle-radius": 9, "circle-color": "#22D3EE",
                     "circle-stroke-color": "#FFFFFF", "circle-stroke-width": 3 } });

        const ms = Math.min(16000, Math.max(6000, (r.length_m || 300) * 14));
        const t0 = performance.now();
        map.easeTo({ center: pts[0], zoom: 18.4, pitch: 60,
                     bearing: bearing(pts[0], at(0.03)), duration: 1600 });
        const step = () => {
            if (drive.current.stop) return;
            const t = Math.min(1, (performance.now() - t0 - 1600) / ms);
            if (t > 0) {
                const c = at(t);
                map.getSource(CAR)?.setData(
                    { type: "Feature", geometry: { type: "Point", coordinates: c } });
                map.easeTo({ center: c, bearing: bearing(c, at(Math.min(1, t + 0.04))),
                             duration: 120, easing: (x) => x });
            }
            if (t < 1) drive.current.raf = requestAnimationFrame(step);
            else {
                map.easeTo({ pitch: 0, duration: 1200 });
                setDriving(null);
            }
        };
        drive.current.raf = requestAnimationFrame(step);
    }, [getMap, stopDrive]);

    useEffect(() => () => { drive.current.stop = true; cancelAnimationFrame(drive.current.raf); }, []);

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
        } catch (e) {
            setTurns((t) => [...t, { role: "ai", text: `Could not answer: ${e.message}`, roads: [] }]);
        } finally {
            setBusy(false);
        }
    };

    if (!gpu.base) return null;

    return (
        <>
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
                                flex-col overflow-hidden rounded-[10px] border border-[var(--line)]
                                bg-[var(--surface-1)] shadow-2xl" style={{ maxHeight: "min(620px, 78vh)" }}>
                    <div className="flex items-center gap-2 border-b border-[var(--line)] px-3 py-2.5">
                        <Sparkles size={14} style={{ color: "var(--accent)" }} />
                        <div className="min-w-0 flex-1">
                            <div className="text-[12px] font-medium">Ask the survey</div>
                            <div className="text-[10px] opacity-60">Guntur roads · answers come from the data</div>
                        </div>
                        {driving && (
                            <button onClick={stopDrive} title="Stop"
                                className="rounded p-1 hover:bg-[var(--surface-2)]"><Square size={13} /></button>
                        )}
                        <button onClick={() => { setOpen(false); stopDrive(); clearMap(); }}
                            className="rounded p-1 hover:bg-[var(--surface-2)]"><X size={14} /></button>
                    </div>

                    <div className="flex-1 space-y-2.5 overflow-y-auto px-3 py-3">
                        {!turns.length && (
                            <div className="space-y-1.5">
                                <div className="text-[11px] opacity-60">Try asking:</div>
                                {SUGGESTIONS.map((s) => (
                                    <button key={s} onClick={() => send(s)}
                                        className="block w-full rounded-[6px] border border-[var(--line)]
                                                   px-2.5 py-1.5 text-left text-[11.5px] hover:bg-[var(--surface-2)]">
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
                                        : "border border-[var(--line)] bg-[var(--surface-2)]"}`}>
                                    {t.text}
                                </div>
                                {!!t.roads?.length && (
                                    <div className="mt-1.5 space-y-1">
                                        {t.roads.slice(0, 6).map((r, k) => (
                                            <div key={r.road + k}
                                                className="flex items-center gap-2 rounded-[6px] border
                                                           border-[var(--line)] px-2 py-1 text-[11px]">
                                                <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                                                    style={{ background: GRADE_COLOUR[r.grade] || "#FF1744" }} />
                                                <span className="min-w-0 flex-1 truncate">{r.road}</span>
                                                <span className="shrink-0 opacity-60">{r.potholes}p · {Math.round(r.length_m)}m</span>
                                                <button title="Zoom to it" onClick={() => show([r])}
                                                    className="shrink-0 rounded p-0.5 hover:bg-[var(--surface-2)]">
                                                    <MapPin size={12} />
                                                </button>
                                                <button title="Drive along it" onClick={() => driveRoad(r)}
                                                    disabled={!(r.path?.length > 1)}
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

                    <div className="flex items-center gap-1.5 border-t border-[var(--line)] p-2">
                        <input value={q} onChange={(e) => setQ(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && send()}
                            placeholder="Which road is worst?"
                            className="min-w-0 flex-1 rounded-[6px] border border-[var(--line)]
                                       bg-[var(--surface-2)] px-2.5 py-1.5 text-[12px] outline-none
                                       focus:border-[var(--accent)]" />
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
