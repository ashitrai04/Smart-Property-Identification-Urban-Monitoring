import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import {
    LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
    ReferenceLine, ResponsiveContainer,
} from "recharts";
import {
    Play, Pause, SkipBack, SkipForward, Crosshair, AlertTriangle,
    Upload as UploadIcon, Gauge, Activity, Ruler, Layers, Maximize2, Minimize2,
} from "lucide-react";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;
const BASE = "/nsv";
// Detection JSON and route GeoJSON ship with the build; the videos are too
// large for the repo and live in Cloudflare R2. Point VITE_NSV_MEDIA_BASE at
// the bucket's public base + "/nsv" in Vercel; unset, it falls back to the
// local public folder, which is what the dev server serves.
const MEDIA = (import.meta.env.VITE_NSV_MEDIA_BASE || BASE).replace(/\/$/, "");

/* The survey measures, each against its own contract limit. The map colours by
   whichever one is selected rather than by a single blended score: on this
   corridor rut depth sits just under its limit almost everywhere, so a blended
   score is really a rut map wearing a disguise. */
const MEASURES = [
    { id: "composite", label: "Worst of four", unit: "", ratio: "worst_ratio", value: null, icon: Layers },
    { id: "roughness", label: "Roughness", unit: "mm/km", ratio: "r_roughness", value: "roughness", icon: Activity },
    { id: "rut", label: "Rut depth", unit: "mm", ratio: "r_rut", value: "rut_mm", icon: Ruler },
    { id: "crack", label: "Cracking", unit: "% area", ratio: "r_crack", value: "crack_pct", icon: AlertTriangle },
    { id: "ravel", label: "Ravelling", unit: "% area", ratio: "r_ravel", value: "ravel_pct", icon: Gauge },
];

const C_GOOD = "#22C55E", C_WARN = "#F5A524", C_BAD = "#EF4444", C_NULL = "#64748B";
const FPS = 5;

const fmtChn = (m) => `${(m / 1000).toFixed(3)} km`;

export default function NSV() {
    const mapEl = useRef(null);
    const map = useRef(null);
    const videoRef = useRef(null);
    const camMarker = useRef(null);
    const vidBoxRef = useRef(null);
    const rafRef = useRef(0);

    const [route, setRoute] = useState(null);
    const [dets, setDets] = useState(null);
    const [boxes, setBoxes] = useState(null);
    const [track, setTrack] = useState(null);
    const [summary, setSummary] = useState(null);
    const [measure, setMeasure] = useState("composite");
    const [playing, setPlaying] = useState(false);
    const [frame, setFrame] = useState(0);
    const [picked, setPicked] = useState(null);
    const [showUnconfirmed, setShowUnconfirmed] = useState(false);
    const [err, setErr] = useState(null);
    const [source, setSource] = useState("nsv");   // "nsv" | "drone"
    const [noVideo, setNoVideo] = useState(false);
    const [drone, setDrone] = useState(null);

    /* ---------- data ---------- */
    useEffect(() => {
        let dead = false;
        (async () => {
            try {
                const [r, d, t, s, b] = await Promise.all([
                    fetch(`${BASE}/nsv_route.geojson`).then((x) => x.json()),
                    fetch(`${BASE}/nsv_detections.geojson`).then((x) => x.json()),
                    fetch(`${BASE}/nsv_track.json`).then((x) => x.json()),
                    fetch(`${BASE}/nsv_summary.json`).then((x) => x.json()),
                    fetch(`${BASE}/nsv_boxes.json`).then((x) => x.json()),
                ]);
                if (dead) return;
                setRoute(r); setDets(d); setTrack(t); setSummary(s); setBoxes(b);
            } catch (e) {
                if (!dead) setErr(String(e));
            }
        })();
        return () => { dead = true; };
    }, []);

    /* Frame -> telemetry. The web video keeps the source's 8,669 frames and
       5 fps exactly, so the index is the join key and no interpolation of
       time is needed. */
    const frames = track?.frames;
    const at = useCallback((f) => {
        if (!frames?.length) return null;
        const i = Math.max(0, Math.min(frames.length - 1, f));
        const [fr, t, chn, lat, lon, spd] = frames[i];
        return { frame: fr, t, chn, lat, lon, spd };
    }, [frames]);
    const cur = at(frame);

    /* ---------- map ---------- */
    useEffect(() => {
        if (!mapEl.current || map.current || !route) return;
        if (!MAPBOX_TOKEN) { setErr("VITE_MAPBOX_TOKEN is not set"); return; }
        mapboxgl.accessToken = MAPBOX_TOKEN;
        const coords = route.features.flatMap((f) => f.geometry.coordinates);
        const b = coords.reduce(
            (a, c) => [Math.min(a[0], c[0]), Math.min(a[1], c[1]), Math.max(a[2], c[0]), Math.max(a[3], c[1])],
            [180, 90, -180, -90]
        );
        const m = new mapboxgl.Map({
            container: mapEl.current,
            style: "mapbox://styles/mapbox/satellite-streets-v12",
            bounds: [[b[0], b[1]], [b[2], b[3]]],
            fitBoundsOptions: { padding: 60 },
        });
        map.current = m;
        m.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right");

        m.on("load", () => {
            m.addSource("route", { type: "geojson", data: route });
            m.addLayer({
                id: "route-casing", type: "line", source: "route",
                paint: { "line-color": "#0B1220", "line-width": 7, "line-opacity": 0.9 },
            });
            m.addLayer({
                id: "route-line", type: "line", source: "route",
                layout: { "line-cap": "round" },
                paint: { "line-width": 4, "line-color": C_GOOD },
            });
            m.addSource("dets", { type: "geojson", data: dets || { type: "FeatureCollection", features: [] } });
            m.addLayer({
                id: "det-halo", type: "circle", source: "dets",
                filter: ["==", ["get", "status"], "confirmed"],
                paint: {
                    "circle-radius": 13, "circle-color": C_BAD,
                    "circle-opacity": 0.18, "circle-blur": 0.5,
                },
            });
            m.addLayer({
                id: "det-dot", type: "circle", source: "dets",
                // set here as well as in the effect below: the effect can run
                // before the style has loaded, in which case it bails out and
                // never fires again, leaving all 171 candidates on the map
                filter: showUnconfirmed ? null : ["==", ["get", "status"], "confirmed"],
                paint: {
                    "circle-radius": ["case", ["==", ["get", "status"], "confirmed"], 6, 4],
                    "circle-color": ["case", ["==", ["get", "status"], "confirmed"], C_BAD, C_WARN],
                    "circle-stroke-width": 1.5, "circle-stroke-color": "#0B1220",
                    "circle-opacity": ["case", ["==", ["get", "status"], "confirmed"], 1, 0.55],
                },
            });

            const el = document.createElement("div");
            el.style.cssText =
                "width:16px;height:16px;border-radius:50%;background:#2DD4BF;" +
                "box-shadow:0 0 0 4px rgba(45,212,191,.25),0 0 14px rgba(45,212,191,.9);" +
                "border:2px solid #0B1220";
            camMarker.current = new mapboxgl.Marker({ element: el }).setLngLat([b[0], b[1]]).addTo(m);

            m.on("click", "det-dot", (e) => {
                const p = e.features[0].properties;
                setPicked(p);
                if (p.frame != null) seekFrame(Number(p.frame));
            });
            m.on("mouseenter", "det-dot", () => { m.getCanvas().style.cursor = "pointer"; });
            m.on("mouseleave", "det-dot", () => { m.getCanvas().style.cursor = ""; });

            // click the route to jump the video to that chainage
            m.on("click", "route-line", (e) => {
                const chn = Number(e.features[0].properties.chn_start);
                jumpToChainage(chn);
            });
            m.on("mouseenter", "route-line", () => { m.getCanvas().style.cursor = "crosshair"; });
            m.on("mouseleave", "route-line", () => { m.getCanvas().style.cursor = ""; });
        });
        // Mapbox measures its container once, at creation. If the grid is still
        // settling at that moment the canvas is sized 0x0 and stays blank for
        // good - which is exactly what happened when this column moved to grid.
        const ro = new ResizeObserver(() => m.resize());
        ro.observe(mapEl.current);
        requestAnimationFrame(() => m.resize());

        return () => { ro.disconnect(); m.remove(); map.current = null; };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [route]);

    /* recolour when the chosen measure changes */
    useEffect(() => {
        const m = map.current;
        if (!m || !m.getLayer?.("route-line")) return;
        const spec = MEASURES.find((x) => x.id === measure);
        const r = ["coalesce", ["get", spec.ratio], -1];
        m.setPaintProperty("route-line", "line-color", [
            "case",
            ["<", r, 0], C_NULL,
            [">", r, 1.0], C_BAD,
            [">=", r, 0.95], C_WARN,
            C_GOOD,
        ]);
    }, [measure, route]);

    /* detection visibility */
    useEffect(() => {
        const m = map.current;
        if (!m || !m.getLayer?.("det-dot")) return;
        m.setFilter("det-dot", showUnconfirmed ? null : ["==", ["get", "status"], "confirmed"]);
    }, [showUnconfirmed, dets]);

    /* move the camera marker with playback */
    useEffect(() => {
        if (!camMarker.current || !cur) return;
        camMarker.current.setLngLat([cur.lon, cur.lat]);
    }, [cur?.lon, cur?.lat]);

    /* drive `frame` off the video clock */
    useEffect(() => {
        const tick = () => {
            const v = videoRef.current;
            if (v && !v.paused && !v.seeking) {
                setFrame(Math.round(v.currentTime * FPS));
            }
            rafRef.current = requestAnimationFrame(tick);
        };
        rafRef.current = requestAnimationFrame(tick);
        return () => cancelAnimationFrame(rafRef.current);
    }, []);

    const seekFrame = useCallback((f) => {
        const v = videoRef.current;
        const n = frames?.length || 1;
        const g = Math.max(0, Math.min(n - 1, f));
        setFrame(g);
        if (v) v.currentTime = g / FPS;
    }, [frames]);

    const jumpToChainage = useCallback((chn) => {
        if (!frames?.length) return;
        let lo = 0, hi = frames.length - 1;
        while (lo < hi) {               // chainage is monotonic, so bisect
            const mid = (lo + hi) >> 1;
            if (frames[mid][2] < chn) lo = mid + 1; else hi = mid;
        }
        seekFrame(lo);
    }, [frames, seekFrame]);

    const toggle = () => {
        const v = videoRef.current;
        if (!v) return;
        if (v.paused) { v.play(); setPlaying(true); } else { v.pause(); setPlaying(false); }
    };

    /* follow the vehicle on the map while playing */
    useEffect(() => {
        if (!playing || !map.current || !cur) return;
        const m = map.current;
        const c = m.getCenter();
        const far = Math.hypot(c.lng - cur.lon, c.lat - cur.lat) > 0.004;
        if (far) m.easeTo({ center: [cur.lon, cur.lat], duration: 600 });
    }, [playing, cur?.lon, cur?.lat]);

    /* Boxes to draw over the video right now.

       Only 207 of the 8,669 frames contain a detection, so a box drawn on
       exactly its own frame flashes for 200 ms and is effectively invisible.
       A +/-1 frame window holds it on screen for about half a second without
       moving it anywhere it was not seen. */
    const liveBoxes = useMemo(() => {
        const bf = boxes?.by_frame;
        if (!bf) return [];
        const out = [];
        for (let f = frame - 1; f <= frame + 1; f++) {
            for (const b of bf[String(f)] || []) {
                const [x0, y0, x1, y1, label, conf, status, id] = b;
                if (!showUnconfirmed && status !== "confirmed") continue;
                if (!out.some((o) => o.id === id)) out.push({ x0, y0, x1, y1, label, conf, status, id });
            }
        }
        return out;
    }, [boxes, frame, showUnconfirmed]);

    /* jump list: every detection, best first, so they can actually be reached */
    const jumps = useMemo(() => {
        const j = boxes?.jumps || [];
        return showUnconfirmed ? j : j.filter((x) => x.status === "confirmed");
    }, [boxes, showUnconfirmed]);

    const stepDetection = useCallback((dir) => {
        if (!jumps.length) return;
        const ordered = [...jumps].sort((a, b) => a.frame - b.frame);
        const next = dir > 0
            ? ordered.find((j) => j.frame > frame + 1)
            : [...ordered].reverse().find((j) => j.frame < frame - 1);
        const t = next || (dir > 0 ? ordered[0] : ordered[ordered.length - 1]);
        seekFrame(t.frame);
        const f = dets?.features?.find((x) => x.properties.defect_id === t.defect_id);
        if (f) {
            setPicked(f.properties);
            map.current?.easeTo({ center: f.geometry.coordinates, zoom: 15, duration: 600 });
        }
    }, [jumps, frame, seekFrame, dets]);

    const stats = summary;
    const counts = stats?.status_counts;

    return (
        <div style={{
            ...S.wrap,
            // the second column exists only to hold the NSV clip; the drone
            // view brings its own layout and needs the full width
            gridTemplateColumns: source === "nsv" ? S.wrap.gridTemplateColumns : "minmax(0,1fr)",
        }}>
            <style>{FS_CSS}</style>
            <div style={S.leftStack}>
            {/* ── header ───────────────────────────────────────── */}
            <div style={S.head}>
                <div>
                    <div style={S.title}>Network Survey Vehicle — {stats?.corridor ?? "…"}</div>
                    <div style={S.sub}>
                        Lane {stats?.lane} · {stats?.length_km ?? "–"} km ·
                        chainage {stats ? `${(stats.chainage_start / 1000).toFixed(1)}–${(stats.chainage_end / 1000).toFixed(1)} km` : "–"} ·
                        {" "}{stats?.blocks ?? "–"} survey blocks
                    </div>
                </div>
                <div style={S.pills}>
                    <Pill tone="ok" n={counts?.good} label="within limit" />
                    <Pill tone="warn" n={counts?.approaching} label="≥95% of limit" />
                    <Pill tone="bad" n={counts?.exceeds} label="over limit" />
                </div>
            </div>

            {err && <div style={S.err}><AlertTriangle size={14} /> {err}</div>}

            <div style={S.srcBar}>
                <button onClick={() => setSource("nsv")}
                    style={{ ...S.srcBtn, ...(source === "nsv" ? S.srcOn : null) }}>
                    NSV forward camera · NH148N · 43 km
                </button>
                <button onClick={() => setSource("drone")}
                    style={{ ...S.srcBtn, ...(source === "drone" ? S.srcOn : null) }}>
                    Drone survey · Kokkiligadda Rd · village road
                </button>

                {source === "nsv" && (
                    <>
                        <span style={S.vsep} />
                        {MEASURES.map((m) => {
                            const Ico = m.icon;
                            const on = measure === m.id;
                            return (
                                <button key={m.id} onClick={() => setMeasure(m.id)}
                                    style={{ ...S.mBtn, ...(on ? S.mBtnOn : null) }}>
                                    <Ico size={12} /> {m.label}
                                </button>
                            );
                        })}
                    </>
                )}
                <div style={{ flex: 1 }} />
                <label style={S.chk}>
                    <input type="checkbox" checked={showUnconfirmed}
                        onChange={(e) => setShowUnconfirmed(e.target.checked)} />
                    show unconfirmed
                </label>
                <UploadButton />
            </div>

            {source === "drone" && <DroneView />}

            {/* ── body ─────────────────────────────────────────── */}
            <div style={{ ...S.body, display: source === "nsv" ? "grid" : "none" }}>
                {/* left column: map above, chart below. The video is portrait
                    (2048x2652), so it needs the full column height to be worth
                    looking at - everything else stacks beside it. */}
                <div style={S.leftCol}>
                <div style={S.mapCol}>
                    <div ref={mapEl} style={S.map} />
                </div>

                    <div style={S.chartRow}>
                        <MeasureChart route={route} cur={cur} onPick={jumpToChainage} />
                        <DetailCard picked={picked} onSeek={seekFrame} />
                    </div>
                </div>

            </div>

            {/* ── condition strip ──────────────────────────────── */}
            {source === "nsv" && (
                <ConditionStrip
                    route={route} measure={measure} cur={cur}
                    onPick={jumpToChainage}
                />
            )}
            </div>

            {/* The video sits in its own full-height column beside everything
                else, header band included. Nested inside the body row it only
                ever got the leftover height; here it gets the whole page. */}
            {source === "nsv" && (
                <div style={S.vidCol}>
                    <div style={S.vidBox} ref={vidBoxRef} className="nsv-fs">
                        <ExpandButton boxRef={vidBoxRef} />
                        <video
                            ref={videoRef}
                            src={`${MEDIA}/L2_web.mp4`}
                            style={S.video}
                            preload="metadata"
                            onPlay={() => setPlaying(true)}
                            onPause={() => setPlaying(false)}
                            onError={() => setNoVideo(true)}
                            playsInline
                        />
                        {noVideo && <VideoMissing name="L2_web.mp4" />}
                        {/* Detection boxes in native 2048x2652 frame pixels. The video is
                            object-fit:contain, so the overlay must letterbox identically —
                            hence meet, not none, or the boxes drift off the defect. */}
                        <svg viewBox="0 0 2048 2652" preserveAspectRatio="xMidYMid meet" style={S.svg}>
                            {liveBoxes.map((b) => {
                                const col = b.status === "confirmed" ? C_BAD : C_WARN;
                                return (
                                    <g key={b.id}>
                                        <rect x={b.x0} y={b.y0} width={b.x1 - b.x0} height={b.y1 - b.y0}
                                            fill="none" stroke={col} strokeWidth="8" />
                                        {/* keep the caption inside the frame when the box
                                            sits against the top edge */}
                                        <rect x={b.x0} y={Math.max(0, b.y0 - 56)}
                                            width={(String(b.label).length + 6) * 26}
                                            height={56} fill={col} opacity="0.9" />
                                        <text x={b.x0 + 8} y={Math.max(42, b.y0 - 14)} fill="#0B1220"
                                            fontSize="40" fontWeight="700" fontFamily="monospace">
                                            {String(b.label).replace(/_/g, " ")} {b.conf.toFixed(2)}
                                        </text>
                                    </g>
                                );
                            })}
                        </svg>
                    </div>

                    {/* Telemetry under the video, not on top of it. As an overlay it
                        covered the carriageway - the one part of the frame anyone is
                        looking at - and sat on the camera's own banner. */}
                    <div style={S.telem}>
                        <Telem k="chainage" v={cur ? fmtChn(cur.chn) : "–"} />
                        <Telem k="lat" v={cur ? cur.lat.toFixed(6) : "–"} />
                        <Telem k="lon" v={cur ? cur.lon.toFixed(6) : "–"} />
                        <Telem k="speed" v={cur ? `${cur.spd} km/h` : "–"} />
                        <Telem k="frame" v={`${frame}/${(frames?.length ?? 1) - 1}`} />
                    </div>

                    <div style={S.controls}>
                        <button style={S.ctl} onClick={() => seekFrame(frame - 25)} title="back 5 s">
                            <SkipBack size={16} />
                        </button>
                        <button style={{ ...S.ctl, ...S.ctlMain }} onClick={toggle}>
                            {playing ? <Pause size={17} /> : <Play size={17} />}
                        </button>
                        <button style={S.ctl} onClick={() => seekFrame(frame + 25)} title="forward 5 s">
                            <SkipForward size={16} />
                        </button>
                        <button style={S.ctl} title="centre the map on the vehicle"
                            onClick={() => cur && map.current?.easeTo({ center: [cur.lon, cur.lat], zoom: 16 })}>
                            <Crosshair size={16} />
                        </button>
                        {/* Detections sit on 207 of 8,669 frames, so playing from the
                            start almost never lands on one. These are how you reach them. */}
                        <button style={S.ctlAlt} onClick={() => stepDetection(-1)} title="previous detection">
                            ◀ det
                        </button>
                        <button style={S.ctlAlt} onClick={() => stepDetection(1)} title="next detection">
                            det ▶
                        </button>
                        <input
                            type="range" min={0} max={(frames?.length ?? 1) - 1} value={frame}
                            onChange={(e) => seekFrame(Number(e.target.value))}
                            style={S.range}
                        />
                    </div>

                </div>
            )}

        </div>
    );
}

/* ───────────────────────── pieces ───────────────────────── */

function Pill({ tone, n, label }) {
    const col = tone === "ok" ? C_GOOD : tone === "warn" ? C_WARN : C_BAD;
    return (
        <div style={{ ...S.pill, borderColor: `${col}55` }}>
            <span style={{ color: col, fontWeight: 700, fontSize: 15 }}>{n ?? "–"}</span>
            <span style={{ color: "var(--text-dim)", fontSize: 11 }}>{label}</span>
        </div>
    );
}

function HudRow({ k, v }) {
    return (
        <div style={{ display: "flex", justifyContent: "space-between", gap: 14 }}>
            <span style={{ color: "var(--text-mute)" }}>{k}</span>
            <span style={{ color: "var(--text)", fontFamily: "var(--font-mono)" }}>{v}</span>
        </div>
    );
}

/* The videos are too large to commit, so a deployed build may not have them.
   Everything else on the page - map, chart, defect list, jump-to-detection -
   runs off the JSON and still works, so say that rather than showing a black
   rectangle that looks like a bug. */
function VideoMissing({ name }) {
    return (
        <div style={S.noVid}>
            <AlertTriangle size={16} style={{ color: "var(--alert)" }} />
            <div style={{ fontWeight: 600, marginTop: 6 }}>{name} not deployed</div>
            <div style={{ color: "var(--text-dim)", marginTop: 4, lineHeight: 1.5 }}>
                The survey videos are hundreds of megabytes and are not committed to
                the repository. Detections, the map and the chart are unaffected.
                Drop the file into <code>public/nsv/</code> to play it here.
            </div>
        </div>
    );
}

function ExpandButton({ boxRef }) {
    const [full, setFull] = useState(false);
    useEffect(() => {
        const h = () => setFull(!!document.fullscreenElement);
        document.addEventListener("fullscreenchange", h);
        return () => document.removeEventListener("fullscreenchange", h);
    }, []);
    const toggle = () => {
        const el = boxRef.current;
        if (!el) return;
        // the frame, not the <video> element: native video fullscreen drops
        // the SVG overlay, so the boxes would vanish exactly when you want
        // a closer look at them
        if (document.fullscreenElement) document.exitFullscreen();
        else el.requestFullscreen?.();
    };
    return (
        <button onClick={toggle} style={S.expand}
            title={full ? "exit full screen (Esc)" : "expand to full screen"}>
            {full ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
    );
}

function Telem({ k, v }) {
    return (
        <div style={S.telemCell}>
            <span style={{ color: "var(--text-mute)", fontSize: 9.5 }}>{k}</span>
            <span style={{ color: "var(--text)", fontFamily: "var(--font-mono)", fontSize: 11.5 }}>{v}</span>
        </div>
    );
}

function Legend() {
    return (
        <div style={S.legend}>
            {[["within limit", C_GOOD], ["≥95% of limit", C_WARN], ["over limit", C_BAD],
            ["confirmed defect", C_BAD], ["vehicle", "#2DD4BF"]].map(([l, c]) => (
                <span key={l} style={S.legItem}>
                    <i style={{ ...S.swatch, background: c }} /> {l}
                </span>
            ))}
        </div>
    );
}

function DetailCard({ picked, onSeek }) {
    if (!picked) {
        return (
            <div style={S.card}>
                <div style={S.cardHint}>
                    Click a defect on the map to inspect it, or click anywhere on the route
                    to jump the video to that chainage.
                </div>
            </div>
        );
    }
    const conf = picked.status === "confirmed";
    return (
        <div style={S.card}>
            <div style={S.cardHead}>
                <span style={{ color: conf ? C_BAD : C_WARN, fontWeight: 700 }}>
                    {String(picked.label || "").replace(/_/g, " ")}
                </span>
                <span style={{ ...S.badge, borderColor: conf ? `${C_BAD}66` : `${C_WARN}66`, color: conf ? C_BAD : C_WARN }}>
                    {picked.status}
                </span>
            </div>
            <HudRow k="confidence" v={Number(picked.confidence).toFixed(2)} />
            <HudRow k="seen in" v={`${picked.seen_in_frames} frame${picked.seen_in_frames > 1 ? "s" : ""}`} />
            <HudRow k="chainage" v={fmtChn(Number(picked.chainage_m))} />
            <HudRow k="range ahead" v={`~${picked.range_ahead_m} m`} />
            <button style={S.jump} onClick={() => onSeek(Number(picked.frame))}>
                jump to this frame
            </button>
            <div style={S.caveat}>
                Pinned at the vehicle position from the telemetry band (±0.6 m).
                The defect lies <i>ahead</i> of that by roughly the range shown, which
                assumes a 2.2 m camera height and an 82° lens — treat it as ±50%.
            </div>
        </div>
    );
}

/* The four survey measures through the chainage window around the vehicle.

   Plotted as a percentage of each measure's own contract limit rather than as
   raw values on a shared axis. The raw numbers span four orders of magnitude -
   roughness runs to 3,463 mm/km while cracking is 0.003% of area - so on one
   axis the distress measures are a flat line on the floor whatever constant
   you multiply them by. Against their own limits all four are comparable, one
   dashed line at 100% is the limit for every series, and the tooltip still
   carries the real measured value in its real unit. */
function MeasureChart({ route, cur, onPick, span = 14 }) {
    const data = useMemo(() => {
        const bs = route?.features ?? [];
        if (!bs.length) return [];
        let i = 0;
        if (cur) {
            i = bs.findIndex((f) => f.properties.chn_end >= cur.chn);
            if (i < 0) i = bs.length - 1;
        }
        const a = Math.max(0, i - Math.floor(span / 2));
        return bs.slice(a, a + span).map((f) => {
            const p = f.properties;
            const pc = (x) => (x == null ? null : Math.round(x * 1000) / 10);
            return {
                chn: p.chn_start,
                km: (p.chn_start / 1000).toFixed(2),
                Roughness: pc(p.r_roughness), Rutting: pc(p.r_rut),
                Cracking: pc(p.r_crack), Ravelling: pc(p.r_ravel),
                _raw: {
                    Roughness: [p.roughness, "mm/km", p.roughness_lim],
                    Rutting: [p.rut_mm, "mm", p.rut_lim],
                    Cracking: [p.crack_pct, "% area", p.crack_lim],
                    Ravelling: [p.ravel_pct, "% area", p.ravel_lim],
                },
            };
        });
    }, [route, cur, span]);

    const series = [
        ["Roughness", "#60A5FA"], ["Rutting", "#F5A524"],
        ["Cracking", "#EF4444"], ["Ravelling", "#2DD4BF"],
    ];

    return (
        <div style={S.chartCard}>
            <div style={S.stripHead}>
                <span>Survey measures — % of contract limit</span>
                <span style={{ color: "var(--text-mute)" }}>
                    {span} blocks around the vehicle
                </span>
            </div>
            <div style={{ flex: 1, minHeight: 180 }}>
                <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={data} margin={{ top: 6, right: 8, bottom: 2, left: -18 }}
                        onClick={(e) => {
                            const p = e?.activePayload?.[0]?.payload;
                            if (p) onPick(p.chn);
                        }}>
                        <CartesianGrid stroke="#2A3A50" strokeDasharray="2 4" />
                        <XAxis dataKey="km" tick={{ fontSize: 9, fill: "#64748B" }}
                            tickLine={false} axisLine={{ stroke: "#2A3A50" }} minTickGap={16} />
                        <YAxis tick={{ fontSize: 9, fill: "#64748B" }} tickLine={false}
                            axisLine={{ stroke: "#2A3A50" }} width={46}
                            domain={[0, (m) => Math.max(120, Math.ceil(m / 20) * 20)]}
                            tickFormatter={(v) => `${v}%`} />
                        <Tooltip
                            contentStyle={{
                                background: "#131C2B", border: "1px solid #2A3A50",
                                borderRadius: 8, fontSize: 11,
                            }}
                            labelFormatter={(v) => `chainage ${v} km`}
                            formatter={(v, name, item) => {
                                const r = item?.payload?._raw?.[name];
                                if (!r || r[0] == null) return [`${v}%`, name];
                                return [`${r[0]} ${r[1]}  (${v}% of ${r[2]})`, name];
                            }} />
                        <ReferenceLine y={100} stroke="#EF4444" strokeDasharray="5 4"
                            label={{ value: "limit", position: "right", fill: "#EF4444", fontSize: 9 }} />
                        {series.map(([k, c]) => (
                            <Line key={k} type="monotone" dataKey={k} stroke={c}
                                strokeWidth={1.8} dot={{ r: 1.8 }} activeDot={{ r: 4 }}
                                isAnimationActive={false} connectNulls />
                        ))}
                        {cur && (
                            <ReferenceLine x={(cur.chn / 1000).toFixed(2)} stroke="#2DD4BF"
                                strokeWidth={1.5} />
                        )}
                    </LineChart>
                </ResponsiveContainer>
            </div>
            <div style={S.legend}>
                {series.map(([k, c]) => (
                    <span key={k} style={S.legItem}><i style={{ ...S.swatch, background: c }} /> {k}</span>
                ))}
            </div>
        </div>
    );
}

/* A 43 km ribbon: every survey block as one slice, coloured by the selected
   measure. Gives the whole corridor at a glance and doubles as a scrubber. */
function ConditionStrip({ route, measure, cur, onPick }) {
    const spec = MEASURES.find((m) => m.id === measure);
    const blocks = route?.features ?? [];
    const pos = useMemo(() => {
        if (!blocks.length || !cur) return 0;
        const a = blocks[0].properties.chn_start;
        const b = blocks[blocks.length - 1].properties.chn_end;
        return Math.max(0, Math.min(1, (cur.chn - a) / (b - a)));
    }, [blocks, cur]);

    return (
        <div style={S.strip}>
            <div style={S.stripHead}>
                <span>Corridor condition — {spec.label}</span>
                <span style={{ display: "flex", gap: 12, alignItems: "center" }}>
                    <Legend />
                    <span style={{ color: "var(--text-mute)" }}>click to jump · {blocks.length} blocks</span>
                </span>
            </div>
            <div style={S.stripBody}>
                {blocks.map((f, i) => {
                    const p = f.properties;
                    const r = p[spec.ratio];
                    const col = r == null ? C_NULL : r > 1 ? C_BAD : r >= 0.95 ? C_WARN : C_GOOD;
                    const v = spec.value ? p[spec.value] : null;
                    return (
                        <div
                            key={i}
                            onClick={() => onPick(p.chn_start)}
                            title={`${(p.chn_start / 1000).toFixed(2)} km — ${spec.label}` +
                                (v != null ? ` ${v} ${spec.unit}` : "") +
                                ` (${r == null ? "no data" : Math.round(r * 100) + "% of limit"})`}
                            style={{ flex: 1, background: col, height: "100%", cursor: "pointer" }}
                        />
                    );
                })}
                <div style={{ ...S.stripCursor, left: `${pos * 100}%` }} />
            </div>
        </div>
    );
}

/* Drone survey view.

   Keyed on playback TIME, not frame number. The source is 50 fps and the web
   copy is resampled to 25, so frame indices do not survive the transcode while
   wall-clock time does; detections are stored against source frames and
   converted at lookup. Getting this wrong puts every box on the wrong half of
   the video.

   There is no map here because there is nothing to put on one: this clip
   carries no telemetry and arrived with no flight log, so a defect can be
   located in the video but not on the ground. */
// The clip opens with about two minutes of establishing aerials over the
// village - rooftops, yards, open country - which is not road survey footage
// and is not what anyone wants to review. Playback starts after it, and
// detections before it are excluded rather than shown and explained away.
const DRONE_START_S = 120;

function DroneView() {
    const vid = useRef(null);
    const boxRef = useRef(null);
    const raf = useRef(0);
    const [data, setData] = useState(null);
    const [t, setT] = useState(0);
    const [playing, setPlaying] = useState(false);
    const [showUnconfirmed, setShowUnconfirmed] = useState(false);
    const [missing, setMissing] = useState(false);
    const [missingVid, setMissingVid] = useState(false);

    useEffect(() => {
        fetch(`${BASE}/drone_boxes.json`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
            .then(setData)
            .catch(() => setMissing(true));
    }, []);

    // jump past the intro once metadata is in
    const seeded = useRef(false);
    useEffect(() => {
        const v = vid.current;
        if (!v || seeded.current) return;
        const go = () => {
            if (seeded.current) return;
            seeded.current = true;
            try { v.currentTime = DRONE_START_S; setT(DRONE_START_S); } catch { /* ignore */ }
        };
        if (v.readyState >= 1) go();
        else v.addEventListener("loadedmetadata", go, { once: true });
    }, [data]);

    useEffect(() => {
        const tick = () => {
            const v = vid.current;
            if (v && !v.paused && !v.seeking) setT(v.currentTime);
            raf.current = requestAnimationFrame(tick);
        };
        raf.current = requestAnimationFrame(tick);
        return () => cancelAnimationFrame(raf.current);
    }, []);

    const srcFps = data?.fps || 50;
    const step = data?.step || 5;
    const [W, H] = data?.native || [1920, 1080];

    const live = useMemo(() => {
        if (!data?.by_frame) return [];
        if (t < DRONE_START_S) return [];
        const f0 = Math.round(t * srcFps);
        const out = [];
        for (let d = -step; d <= step; d++) {
            const k = String(Math.round((f0 + d) / step) * step);
            for (const b of data.by_frame[k] || []) {
                const [x0, y0, x1, y1, label, conf, status, id] = b;
                if (!showUnconfirmed && status !== "confirmed") continue;
                if (!out.some((o) => o.id === id)) out.push({ x0, y0, x1, y1, label, conf, status, id });
            }
        }
        return out;
    }, [data, t, srcFps, step, showUnconfirmed]);

    const jumps = useMemo(() => {
        const j = (data?.jumps || []).filter((x) => x.t_sec >= DRONE_START_S);
        return showUnconfirmed ? j : j.filter((x) => x.status === "confirmed");
    }, [data, showUnconfirmed]);

    const seek = (sec) => {
        const v = vid.current;
        const g = Math.max(DRONE_START_S, sec);
        if (v) { v.currentTime = g; setT(g); }
    };
    const stepDet = (dir) => {
        if (!jumps.length) return;
        const ord = [...jumps].sort((a, b) => a.t_sec - b.t_sec);
        const nxt = dir > 0 ? ord.find((j) => j.t_sec > t + 0.4)
            : [...ord].reverse().find((j) => j.t_sec < t - 0.4);
        seek((nxt || (dir > 0 ? ord[0] : ord[ord.length - 1])).t_sec);
    };

    if (missing) {
        return (
            <div style={{ ...S.card, padding: 16 }}>
                <div style={S.cardHint}>
                    Drone detections not built yet — run <code>drone_detect.py</code>, then copy
                    <code> drone_boxes.json</code> and <code>drone_web.mp4</code> into <code>/public/nsv</code>.
                </div>
            </div>
        );
    }

    const confirmed = jumps.filter((j) => j.status === "confirmed").length;
    const total = data?.jumps?.length ?? 0;

    return (
        <div style={S.droneWrap}>
            <div style={S.droneVid} ref={boxRef} className="nsv-fs">
                <ExpandButton boxRef={boxRef} />
                <video ref={vid} src={`${MEDIA}/drone_web.mp4`} style={S.video}
                    preload="metadata" playsInline
                    onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                    onError={() => setMissingVid(true)} />
                {missingVid && <VideoMissing name="drone_web.mp4" />}
                <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" style={S.svg}>
                    {live.map((b) => {
                        const col = b.status === "confirmed" ? C_BAD : C_WARN;
                        return (
                            <g key={b.id}>
                                <rect x={b.x0} y={b.y0} width={b.x1 - b.x0} height={b.y1 - b.y0}
                                    fill="none" stroke={col} strokeWidth="4" />
                                <rect x={b.x0} y={Math.max(0, b.y0 - 26)}
                                    width={(String(b.label).length + 6) * 11}
                                    height={26} fill={col} opacity="0.92" />
                                <text x={b.x0 + 5} y={Math.max(19, b.y0 - 7)} fill="#0B1220" fontSize="17"
                                    fontWeight="700" fontFamily="monospace">
                                    {String(b.label).replace(/_/g, " ")} {b.conf.toFixed(2)}
                                </text>
                            </g>
                        );
                    })}
                </svg>
            </div>

            <div style={S.droneSide}>
                <div style={S.telem}>
                    <Telem k="time" v={`${t.toFixed(1)} s`} />
                    <Telem k="on screen" v={String(live.length)} />
                    <Telem k="position" v="no telemetry" />
                </div>
                <div style={S.card}>
                    <div style={S.cardHead}>
                        <span style={{ fontWeight: 700, color: "var(--text)" }}>Drone survey</span>
                        <span style={{ ...S.badge, borderColor: "var(--line)", color: "var(--text-dim)" }}>
                            nadir
                        </span>
                    </div>
                    <HudRow k="defects tracked" v={String(total)} />
                    <HudRow k="confirmed" v={String(confirmed)} />
                    <HudRow k="source" v={`${W}×${H} @${srcFps}fps`} />
                    <HudRow k="sampled" v={`every ${step}th frame`} />
                    <div style={S.caveat}>
                        This clip carries no telemetry banner and came with no flight log, so
                        detections are located <i>in the video</i> and cannot be put on the map.
                        A DJI <code>.SRT</code> sidecar or the flight CSV would geotag every one
                        of them with no other change.
                    </div>
                </div>

                <div style={S.controls}>
                    <button style={{ ...S.ctl, ...S.ctlMain }}
                        onClick={() => { const v = vid.current; if (!v) return; v.paused ? v.play() : v.pause(); }}>
                        {playing ? <Pause size={17} /> : <Play size={17} />}
                    </button>
                    <button style={S.ctlAlt} onClick={() => stepDet(-1)}>◀ det</button>
                    <button style={S.ctlAlt} onClick={() => stepDet(1)}>det ▶</button>
                    <label style={S.chk}>
                        <input type="checkbox" checked={showUnconfirmed}
                            onChange={(e) => setShowUnconfirmed(e.target.checked)} />
                        unconfirmed
                    </label>
                </div>

                <div style={{ ...S.card, flex: 1, minHeight: 0, overflowY: "auto" }}>
                    <div style={{ color: "var(--text-dim)", fontSize: 10.5, marginBottom: 4 }}>
                        detections — click to jump
                    </div>
                    {jumps.slice(0, 120).map((j) => (
                        <button key={j.id} onClick={() => seek(j.t_sec)} style={S.detRow}>
                            <span style={{ color: j.status === "confirmed" ? C_BAD : C_WARN }}>
                                {String(j.label).replace(/_/g, " ")}
                            </span>
                            <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-dim)" }}>
                                {j.conf.toFixed(2)} · {j.t_sec.toFixed(0)}s
                            </span>
                        </button>
                    ))}
                    {!jumps.length && (
                        <div style={S.cardHint}>
                            No confirmed defects at this threshold — tick “unconfirmed” to see
                            every candidate.
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function UploadButton() {
    const [file, setFile] = useState(null);
    return (
        <label style={{ ...S.iconBtn, ...(file ? S.iconBtnOn : null) }} title={file
            ? `${file.name} queued — run nsv_ocr.py then nsv_detect.py and drop the outputs in /public/nsv`
            : "Upload a survey video. Any NSV clip with the telemetry banner geotags itself — no GPS log needed."}>
            <UploadIcon size={14} />
            <input type="file" accept="video/*" style={{ display: "none" }}
                onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
    );
}

function UploadNote() {
    const [file, setFile] = useState(null);
    return (
        <div style={S.upload}>
            <UploadIcon size={13} style={{ color: "var(--signal)", flexShrink: 0 }} />
            <div style={{ flex: 1, fontSize: 11, color: "var(--text-dim)", lineHeight: 1.35 }}>
                {file
                    ? `${file.name} — ${(file.size / 1e6).toFixed(0)} MB. Run nsv_ocr.py then nsv_detect.py, and drop the outputs in /public/nsv.`
                    : "Process your own survey video — any NSV clip with the telemetry banner geotags itself, no GPS log needed."}
            </div>
            <label style={S.pickBtn}>
                choose file
                <input type="file" accept="video/*" style={{ display: "none" }}
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </label>
        </div>
    );
}

/* Fullscreen: the frame keeps an aspect-ratio and a fixed height on the page,
   which would letterbox it inside the screen. Reset both while expanded. */
const FS_CSS = `
.nsv-fs:fullscreen { width:100vw; height:100vh; max-width:100vw; max-height:100vh;
  aspect-ratio:auto; border:0; border-radius:0; background:#000; margin:0; }
.nsv-fs:fullscreen video { width:100%; height:100%; object-fit:contain; }
`;

/* ───────────────────────── styles ───────────────────────── */
const S = {
    // Explicit grid rows rather than a flex column. With flex, the map's
    // flex:1 and the chart's percentage height both resolved against a parent
    // that never got minHeight:0, so the column overflowed and the Mapbox
    // canvas - which is absolutely positioned - ended up underneath the chart.
    // Two page columns. The video occupies the right one for the full height
    // of the page - header band included - because a portrait clip is only
    // legible when it is tall, and nested inside the body row it was limited
    // to whatever height the header and the condition strip left over.
    wrap: {
        display: "grid",
        gridTemplateColumns:
            "minmax(0,1fr) min(44vw, calc((100vh - 156px) * 0.7722))",
        gap: 10, height: "100%", padding: 12, minHeight: 0, overflow: "hidden",
    },
    leftStack: {
        display: "flex", flexDirection: "column", gap: 9,
        minHeight: 0, minWidth: 0, overflow: "hidden",
    },
    head: { display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" },
    title: { fontFamily: "var(--font-display)", fontSize: 18, color: "var(--text)", fontWeight: 600 },
    sub: { fontSize: 11.5, color: "var(--text-dim)", marginTop: 3 },
    pills: { display: "flex", gap: 8, flexShrink: 0 },
    pill: {
        display: "flex", flexDirection: "column", alignItems: "center", gap: 1,
        padding: "5px 12px", borderWidth: 1, borderStyle: "solid",
        borderRadius: "var(--r-md)",
        background: "var(--surface)", minWidth: 86,
    },
    err: {
        display: "flex", alignItems: "center", gap: 8, padding: "7px 10px",
        background: "var(--critical-dim)", border: "1px solid rgba(239,68,68,.35)",
        borderRadius: "var(--r-md)", color: "#FCA5A5", fontSize: 12,
    },
    // Two columns, not three. The video is portrait 2048x2652: in a short wide
    // box it letterboxes away to nothing, so it gets its own full-height column
    // sized near its own aspect ratio, and map + chart stack to the left of it.
    body: {
        display: "grid",
        gridTemplateColumns: "minmax(0,1fr)",
        gap: 10, flex: 1, minHeight: 0,
    },
    leftCol: {
        display: "grid",
        // two children: the map block and the chart row
        gridTemplateRows: "minmax(180px,1.3fr) minmax(165px,0.7fr)",
        gap: 8, minHeight: 0, minWidth: 0, overflow: "hidden",
    },
    chartRow: {
        display: "grid", gridTemplateColumns: "minmax(0,1.4fr) minmax(200px,0.6fr)",
        gap: 8, minHeight: 0, overflow: "hidden",
    },
    chartCard: {
        background: "var(--surface)", border: "1px solid var(--line)",
        borderRadius: "var(--r-lg)", padding: "7px 9px 8px",
        display: "grid", gridTemplateRows: "auto minmax(0,1fr) auto",
        gap: 4, minHeight: 0, overflow: "hidden",
    },
    // three children: measure bar, the map itself, the legend
    mapCol: { display: "grid", gridTemplateRows: "minmax(0,1fr)", minHeight: 0, minWidth: 0, overflow: "hidden" },
    measureBar: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" },
    mBtn: {
        display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 10px",
        fontSize: 11.5, background: "var(--surface)", color: "var(--text-dim)",
        border: "1px solid var(--line)", borderRadius: 999, cursor: "pointer",
    },
    mBtnOn: { background: "var(--signal-dim)", color: "var(--signal)", borderColor: "var(--signal)" },
    chk: { display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--text-dim)", cursor: "pointer" },
    map: { minHeight: 0, height: "100%", borderRadius: "var(--r-lg)", overflow: "hidden", border: "1px solid var(--line)" },
    legend: { display: "flex", gap: 14, flexWrap: "wrap", fontSize: 10.5, color: "var(--text-dim)" },
    legItem: { display: "inline-flex", alignItems: "center", gap: 5 },
    swatch: { width: 9, height: 9, borderRadius: 2, display: "inline-block" },

    vidCol: { display: "grid", gridTemplateRows: "minmax(0,1fr) auto auto", gap: 7, minHeight: 0, minWidth: 0 },
    noVid: {
        position: "absolute", inset: 0, zIndex: 2, display: "flex",
        flexDirection: "column", alignItems: "center", justifyContent: "center",
        textAlign: "center", padding: 24, fontSize: 12, color: "var(--text)",
        background: "var(--surface)",
    },
    expand: {
        position: "absolute", right: 8, top: 8, zIndex: 3,
        width: 28, height: 28, display: "inline-flex",
        alignItems: "center", justifyContent: "center", cursor: "pointer",
        background: "rgba(11,18,32,.72)", color: "var(--text)",
        border: "1px solid var(--line)", borderRadius: "var(--r-md)",
        backdropFilter: "blur(6px)",
    },
    telem: {
        display: "flex", gap: 2, background: "var(--surface)",
        border: "1px solid var(--line)", borderRadius: "var(--r-md)",
        padding: "5px 8px", overflow: "hidden",
    },
    telemCell: {
        display: "flex", flexDirection: "column", gap: 1, flex: 1,
        minWidth: 0, whiteSpace: "nowrap",
    },
    // The clip is portrait 2048x2652. Stretched to the column width it sat in
    // a wide black letterbox, so the frame hugs the video instead: full height,
    // width follows the aspect ratio, centred in the column.
    vidBox: {
        position: "relative", background: "var(--surface)",
        borderRadius: "var(--r-lg)", overflow: "hidden",
        border: "1px solid var(--line)", minHeight: 0, height: "100%",
        // aspect-ratio sizes the frame to the clip exactly: full height, width
        // derived. fit-content collapsed it to the file's intrinsic pixels
        // instead of scaling up to the space available.
        height: "100%", aspectRatio: "2048 / 2652",
        maxWidth: "100%", margin: "0 auto",
        display: "flex", alignItems: "center", justifyContent: "center",
    },
    video: { width: "100%", height: "100%", objectFit: "contain", display: "block" },
    svg: { position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" },
    ctlAlt: {
        height: 32, padding: "0 10px", fontSize: 11, cursor: "pointer",
        background: "var(--surface)", color: "var(--alert)",
        border: "1px solid rgba(245,165,36,.4)", borderRadius: "var(--r-md)",
        whiteSpace: "nowrap", fontFamily: "var(--font-mono)",
    },
    // bottom-left, not top: the video carries its own telemetry banner across
    // the top of every frame and the two were sitting on top of each other
    hud: {
        position: "absolute", left: 8, bottom: 8, padding: "7px 10px",
        background: "rgba(11,18,32,.82)", border: "1px solid var(--line)",
        borderRadius: "var(--r-md)", fontSize: 10.5, display: "flex",
        flexDirection: "column", gap: 3, minWidth: 190, backdropFilter: "blur(6px)",
    },
    controls: { display: "flex", alignItems: "center", gap: 6 },
    ctl: {
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        width: 32, height: 32, background: "var(--surface)", color: "var(--text-dim)",
        border: "1px solid var(--line)", borderRadius: "var(--r-md)", cursor: "pointer",
    },
    ctlMain: { background: "var(--signal-dim)", color: "var(--signal)", borderColor: "var(--signal)" },
    range: { flex: 1, accentColor: "#2DD4BF", cursor: "pointer" },

    card: {
        background: "var(--surface)", border: "1px solid var(--line)",
        borderRadius: "var(--r-lg)", padding: 10, fontSize: 11,
        display: "flex", flexDirection: "column", gap: 5,
        minHeight: 0, overflowY: "auto",
    },
    cardHead: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 2 },
    cardHint: { color: "var(--text-dim)", fontSize: 11, lineHeight: 1.5 },
    badge: {
        fontSize: 9.5, padding: "2px 7px", borderWidth: 1, borderStyle: "solid",
        borderRadius: 999, textTransform: "uppercase", letterSpacing: .4,
    },
    jump: {
        marginTop: 4, padding: "5px 8px", fontSize: 11, cursor: "pointer",
        background: "var(--surface-2)", color: "var(--text)",
        border: "1px solid var(--line)", borderRadius: "var(--r-md)",
    },
    caveat: { marginTop: 4, fontSize: 10, lineHeight: 1.45, color: "var(--text-mute)" },

    strip: {
        background: "var(--surface)", border: "1px solid var(--line)",
        borderRadius: "var(--r-lg)", padding: "5px 10px 7px", flexShrink: 0,
    },
    stripHead: { display: "flex", justifyContent: "space-between", fontSize: 10.5, color: "var(--text-dim)", marginBottom: 4 },
    stripBody: { position: "relative", display: "flex", height: 13, gap: 1, borderRadius: 4, overflow: "hidden" },
    stripCursor: {
        position: "absolute", top: -3, bottom: -3, width: 2, background: "#2DD4BF",
        boxShadow: "0 0 8px rgba(45,212,191,.9)", pointerEvents: "none",
    },

    srcBar: { display: "flex", gap: 6, alignItems: "center", flexWrap: "nowrap", overflow: "hidden" },
    iconBtn: {
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        width: 30, height: 28, flexShrink: 0, cursor: "pointer",
        background: "var(--surface)", color: "var(--text-dim)",
        border: "1px solid var(--line)", borderRadius: "var(--r-md)",
    },
    iconBtnOn: { color: "var(--signal)", borderColor: "var(--signal)", background: "var(--signal-dim)" },
    vsep: { width: 1, height: 18, background: "var(--line)", margin: "0 3px" },
    srcBtn: {
        padding: "6px 14px", fontSize: 11.5, cursor: "pointer",
        background: "var(--surface)", color: "var(--text-dim)",
        border: "1px solid var(--line)", borderRadius: "var(--r-md)",
    },
    srcOn: { background: "var(--signal-dim)", color: "var(--signal)", borderColor: "var(--signal)" },
    droneWrap: {
        display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(300px,420px)",
        gap: 10, flex: 1, minHeight: 0, overflow: "hidden",
    },
    // Shrink-wrap the frame to the clip. The NSV video is portrait and this
    // one is landscape; a fixed box letterboxes whichever one it does not suit.
    droneVid: {
        position: "relative", background: "var(--surface)",
        borderRadius: "var(--r-lg)", overflow: "hidden",
        border: "1px solid var(--line)", minHeight: 0, minWidth: 0,
        // 16:9 here, unlike the NSV clip's 2048x2652 portrait. Sizing both
        // frames the same way letterboxed whichever one did not match.
        width: "100%", aspectRatio: "16 / 9",
        maxHeight: "100%", alignSelf: "center", justifySelf: "center",
        display: "flex", alignItems: "center", justifyContent: "center",
    },
    droneSide: { display: "flex", flexDirection: "column", gap: 8, minHeight: 0 },
    detRow: {
        display: "flex", justifyContent: "space-between", width: "100%",
        padding: "4px 6px", fontSize: 10.5, cursor: "pointer", textAlign: "left",
        background: "transparent", color: "var(--text)",
        border: "1px solid transparent", borderBottom: "1px solid var(--line-soft)",
    },
    upload: {
        display: "flex", alignItems: "center", gap: 9, padding: "5px 10px",
        background: "var(--surface)", border: "1px dashed var(--line)",
        borderRadius: "var(--r-lg)", flexShrink: 0,
    },
    pickBtn: {
        fontSize: 11, padding: "6px 12px", cursor: "pointer",
        background: "var(--surface-2)", color: "var(--text)",
        border: "1px solid var(--line)", borderRadius: "var(--r-md)", whiteSpace: "nowrap",
    },
};
