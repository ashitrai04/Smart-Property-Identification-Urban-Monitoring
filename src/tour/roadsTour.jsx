// "AI for Roads, Bridges & Tunnels" walkthrough — pavement distress detection from
// drones, end to end: the model, live detection on drone road images, then a story
// map of the Guntur drone survey filtered to the ground-truth roads.
import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Cpu, Database, Map as MapIcon, Plane, Route, ScanSearch, ShieldCheck, TrainFront, Building2, Zap } from "lucide-react";
import { callTour, sleep } from "./tourBus";

const SAMPLES = [
    { url: "/tour/roads/road_sample_1.jpg", name: "drone_road_tile_0186.jpg", cache: "/tour/roads/road_sample_1.result.json" },
    { url: "/tour/roads/road_sample_2.jpg", name: "drone_road_tile_0008.jpg", cache: "/tour/roads/road_sample_2.result.json" },
];

// Numbers below come from the real runs (Guntur drone survey, ground-truth road shapefile).
const STOPS = [
    { title: "Hotspot 1 — Arterial junction", stats: [["Potholes", "39"], ["Peak confidence", "0.96"], ["Mean size", "1.2 m"], ["Within", "90 m"]],
      desc: "The densest cluster: <strong>39 verified potholes within 90 m</strong> of a busy junction. Zooming in on the strongest one (<strong>0.96</strong>) and switching the layers <strong>off</strong> shows the raw drone image — the pothole is really there." },
    { title: "Hotspot 2 — Residential grid", stats: [["Potholes", "30"], ["Peak confidence", "0.83"], ["Mean size", "1.2 m"], ["Within", "90 m"]],
      desc: "Narrow inner streets with <strong>30 verified potholes</strong>, every one on the carriageway. Layers off, layers on — check it against the drone image yourself." },
    { title: "Hotspot 3 — Larger failures", stats: [["Potholes", "26"], ["Peak confidence", "0.92"], ["Mean size", "1.9 m"], ["Within", "90 m"]],
      desc: "Bigger defects (mean <strong>1.9 m</strong>). The 4 cm ground resolution gives every pothole a real size in metres — for severity ranking and material estimates." },
    { title: "Hotspot 4 — West approach", stats: [["Potholes", "20"], ["Peak confidence", "0.93"], ["Mean size", "1.1 m"], ["Within", "90 m"]],
      desc: "The western approach road: <strong>20 verified potholes</strong>. The strongest (<strong>0.93</strong>, 1.7 m) checked against the raw drone image — then straight onto the repair list." },
];

export const ROADS_STEPS = [
    { id: "r-intro", hero: "intro", title: "AI for Roads, Bridges & Tunnels", hold: 9000 },
    { id: "r-pipeline", hero: "pipeline", title: "From drone flight to repair list", hold: 11000 },
    { id: "r-model", hero: "model", title: "The road-damage model", hold: 12500 },

    { id: "r-seg", route: "/upload", target: '[data-tour="atype-segment"]', title: "Step 1 — Find the roads",
      desc: "Before judging damage we need to know <strong>where the road is</strong>. Our in-house <strong>SegFormer-B5</strong> (trained on Andhra Pradesh imagery) with <strong>SAM2</strong> maps every road, building, water body and open plot in a drone image.",
      script: [
          { sel: '[data-tour="atype-segment"]', click: true, wait: 900 },
          { sel: '[data-tour="upload-zone"]', run: async () => { await callTour("upload", "showCachedSeg"); }, wait: 1500 },
      ], hold: 2000 },

    { id: "r-seg-result", route: "/upload", target: 'img[alt^="Mask"]', title: "Road network extracted",
      desc: "Two 5 cm Guntur drone chips: <strong>roads in yellow</strong> (a quarter of this chip), every building as its own coloured footprint. This road layer (and the city's ground-truth road map) is what keeps pothole detections <strong>on the carriageway</strong> — never on a roof.",
      waitFor: { sel: 'img[alt^="Mask"]', timeout: 60000 }, postTarget: 'img[alt^="Mask"]', hold: 6500 },

    { id: "r-analysis", route: "/upload", target: '[data-tour="atype-pothole"]', title: "Pavement Distress Detection",
      desc: "In <strong>Analysis</strong>, choose <strong>Pothole Detection</strong>. Any drone or phone image of a road goes in; the YOLOv8 detector returns every pothole and crack with a confidence score.",
      script: [{ sel: '[data-tour="atype-pothole"]', click: true, wait: 1200 }], hold: 3500 },

    { id: "r-load", route: "/upload", target: '[data-tour="pothole-panel"]', title: "Two drone road tiles",
      desc: "Loading two <strong>drone road tiles</strong> (512 px and 1024 px). <strong>Auto</strong> mode lets the model pick the confidence per image — it starts strict and relaxes only until damage is found.",
      script: [{ sel: '[data-tour="pothole-panel"]', run: async () => {
          await callTour("pothole", "setMode", "auto");
          await callTour("pothole", "setRoadsOnly", false);     // close-up road tiles: no road mask needed
          await callTour("pothole", "loadSamples", SAMPLES);
      }, wait: 1500 }], hold: 2500 },

    { id: "r-run", route: "/upload", target: '[data-tour="pothole-run"]', title: "Run the detector",
      desc: "One click sends both images to the <strong>GPU inference server</strong> (NVIDIA A100), with a CPU fallback in the cloud. Large orthophotos are tiled at 1024 px automatically.",
      script: [{ sel: '[data-tour="pothole-run"]', click: true, wait: 600 }],
      waitFor: { check: () => document.querySelectorAll('img[alt="Pothole detection output"]').length >= 2, timeout: 120000 },
      postTarget: '[data-tour="pothole-detections"]', hold: 2500 },

    { id: "r-result", route: "/upload", target: '[data-tour="pothole-result-0"]', title: "Image 1 — one pothole, 87 %",
      desc: "Each detection comes back with its <strong>class, confidence and pixel size</strong> — boxes drawn on the image, a chip for every one below. Results download in one click for inspection reports.",
      hold: 5500 },

    { id: "r-result-2", route: "/upload", target: '[data-tour="pothole-result-1"]', title: "Image 2 — three potholes",
      desc: "The larger tile: <strong>three potholes</strong> at 93 %, 73 % and 67 % confidence, side by side on a shared lane — the kind of cluster that becomes one repair job.",
      script: [{ sel: '[data-tour="pothole-result-1"]', silent: true, wait: 600 }], hold: 6000 },

    { id: "r-map", route: "/mapping", target: '[data-tour="map-canvas"]', title: "Scale it up — a whole city from the air",
      desc: "Now the same model over the <strong>Guntur drone survey</strong>: a 4 cm orthomosaic streamed as map tiles. Every red dot is one <strong>verified</strong> road defect.",
      stats: [["Ground resolution", "4 cm"], ["Survey area", "≈ 3.7 km²"], ["Verified defects", "1,300"], ["On roofs", "0"]],
      script: [{ run: async () => {
          await callTour("mapping", "setBaseMap", "satellite-streets-v12");
          await callTour("mapping", "waitIdle");
          await sleep(600);
          await callTour("mapping", "roadsStory", "start");
      } }], hold: 4000 },

    { id: "r-filter", route: "/mapping", target: '[data-tour="map-canvas"]', title: "Verified against ground-truth roads",
      desc: "Every defect is checked against Guntur's <strong>ground-truth road shapefile</strong> (cyan edges): at least 30 % of it on the road, under 30 % on any roof, and pothole-sized (0.15 – 4 m). Only what passes is on the map.",
      stats: [["Road coverage", "≥ 30 %"], ["Roof overlap", "< 30 %"], ["Pothole size", "0.15–4 m"], ["Verified", "1,300"]],
      script: [{ run: async () => { await callTour("mapping", "roadsStory", "filter"); } }], hold: 5000 },

    ...STOPS.map((s, i) => ({
        id: `r-stop-${i}`, route: "/mapping", target: '[data-tour="map-canvas"]', title: s.title, desc: s.desc, stats: s.stats,
        script: [{ run: async () => { await callTour("mapping", "roadsStory", "stop", i); } }], hold: 1800,
    })),

    { id: "r-overview", route: "/mapping", target: '[data-tour="map-canvas"]', title: "The whole picture",
      desc: "Back to the full survey: <strong>1,300 road defects</strong> located to the centimetre — 1,278 potholes, 9 longitudinal cracks, 6 transverse cracks and 7 repaired patches — ready to prioritise.",
      stats: [["Potholes", "1,278"], ["Longitudinal cracks", "9"], ["Transverse cracks", "6"], ["Repaired patches", "7"]],
      script: [{ run: async () => { await callTour("mapping", "roadsStory", "end"); } }], hold: 5000 },

    { id: "r-impact", hero: "impact", title: "What it delivers", hold: 12000 },
    { id: "r-outro", hero: "outro", title: "Smart roads, from the air", hold: 8000 },
];

// ── count-up number ──
function CountUp({ to, ms = 1400, suffix = "" }) {
    const [v, setV] = useState(0);
    useEffect(() => {
        let raf, t0;
        const step = (t) => {
            t0 ??= t;
            const k = Math.min(1, (t - t0) / ms);
            setV(Math.round(to * (1 - Math.pow(1 - k, 3))));
            if (k < 1) raf = requestAnimationFrame(step);
        };
        raf = requestAnimationFrame(step);
        return () => cancelAnimationFrame(raf);
    }, [to, ms]);
    return <>{v.toLocaleString()}{suffix}</>;
}

const rise = (i) => ({ initial: { opacity: 0, y: 14 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.15 + i * 0.12, duration: 0.45 } });

function Intro() {
    const pillars = [
        [ScanSearch, "Pavement distress", "Potholes & cracks from drone imagery", "live"],
        [Building2, "Construction monitoring", "Change detection 2017 → 2026", "live"],
        [ShieldCheck, "Structural health", "Same crack classes on bridge decks", "next"],
        [TrainFront, "Tunnel intelligence", "Lining cracks & seepage from video", "next"],
    ];
    return (
        <div className="rh-intro">
            <motion.div {...rise(0)} className="rh-kicker">Computer vision · Drones · Edge AI</motion.div>
            <motion.h1 {...rise(1)} className="rh-title">AI for Roads, Bridges <span>&amp; Tunnels</span></motion.h1>
            <motion.p {...rise(2)} className="rh-sub">An AI pipeline that turns drone flights into a geo-located, verified list of road defects — on a live city map.</motion.p>
            <div className="rh-pillars">
                {pillars.map(([Icon, t, d, s], i) => (
                    <motion.div key={t} {...rise(3 + i)} className={`rh-pillar ${s}`}>
                        <Icon size={20} />
                        <div className="rh-pillar-t">{t}</div>
                        <div className="rh-pillar-d">{d}</div>
                        <span className="rh-pillar-s">{s === "live" ? "Live in this demo" : "Same pipeline · next"}</span>
                    </motion.div>
                ))}
            </div>
        </div>
    );
}

function Pipeline() {
    const nodes = [
        [Plane, "Drone capture", "3–5 cm orthomosaics, tens of GB per city"],
        [Database, "Cloud-optimised tiles", "Streamed to the map, read window by window"],
        [Cpu, "YOLOv8 detector", "GPU server · fp16 · tiled 1024 px"],
        [Route, "Ground-truth gate", "Road shapefile · roof & size checks"],
        [MapIcon, "Map & repair list", "Every defect geo-located · CSV / GeoJSON"],
    ];
    return (
        <div className="rh-pipe">
            <motion.div {...rise(0)} className="rh-kicker">The pipeline</motion.div>
            <motion.h2 {...rise(1)} className="rh-h2">From drone flight to repair list</motion.h2>
            <div className="rh-flow">
                {nodes.map(([Icon, t, d], i) => (
                    <div key={t} className="rh-flow-item">
                        <motion.div className="rh-node" initial={{ opacity: 0, scale: 0.7 }} animate={{ opacity: 1, scale: 1 }}
                            transition={{ delay: 0.35 + i * 0.45, type: "spring", stiffness: 220, damping: 18 }}>
                            <div className="rh-node-icon"><Icon size={22} /></div>
                            <div className="rh-node-t">{t}</div>
                            <div className="rh-node-d">{d}</div>
                        </motion.div>
                        {i < nodes.length - 1 && (
                            <motion.div className="rh-link" initial={{ scaleX: 0 }} animate={{ scaleX: 1 }}
                                transition={{ delay: 0.6 + i * 0.45, duration: 0.4 }}><span className="rh-packet" /></motion.div>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}

function Model() {
    const classes = [["D00", "Longitudinal crack"], ["D10", "Transverse crack"], ["D20", "Alligator crack"], ["D40", "Pothole"], ["Repair", "Repaired patch"], ["potholes", "Pothole (drone)"]];
    const specs = [["Architecture", "YOLOv8s · one-stage detector"], ["Input", "1024 px · tiled with 20 % overlap"], ["Weights", "22.6 MB — fits edge devices"],
        ["Merging", "Class-wise NMS across tiles"], ["Confidence", "Auto: 0.50 → 0.15, step 0.05"], ["Runtime", "A100 GPU (fp16) · CPU fallback"]];
    return (
        <div className="rh-model">
            <motion.div {...rise(0)} className="rh-kicker">Model card</motion.div>
            <motion.h2 {...rise(1)} className="rh-h2">Road-damage detector</motion.h2>
            <div className="rh-model-grid">
                <motion.div {...rise(2)} className="rh-card">
                    <div className="rh-card-h">Specification</div>
                    {specs.map(([k, v]) => <div key={k} className="rh-row"><span>{k}</span><b>{v}</b></div>)}
                </motion.div>
                <motion.div {...rise(3)} className="rh-card">
                    <div className="rh-card-h">6 damage classes</div>
                    {classes.map(([c, l], i) => (
                        <motion.div key={c} className="rh-class" initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.7 + i * 0.12 }}>
                            <span className="rh-class-code">{c}</span>{l}
                        </motion.div>
                    ))}
                </motion.div>
                <motion.div {...rise(4)} className="rh-card">
                    <div className="rh-card-h">Verification on the map</div>
                    <div className="rh-row"><span>On ground-truth road</span><b>≥ 30 % of box</b></div>
                    <div className="rh-row"><span>On a roof</span><b>&lt; 30 % of box</b></div>
                    <div className="rh-row"><span>Pothole size</span><b>0.15 – 4 m</b></div>
                    <div className="rh-row"><span>Crack length</span><b>0.2 – 25 m</b></div>
                    <div className="rh-note"><Zap size={12} /> Rules use the image's real ground resolution, so every defect gets a size in metres.</div>
                </motion.div>
            </div>
        </div>
    );
}

function Impact() {
    const nums = [[1300, "verified road defects", ""], [1278, "potholes", ""], [96, "peak confidence", "%"], [4, "cm ground resolution", ""]];
    const out = [["Repair list", "CSV with lat / lon, class, size, confidence — straight into work orders"],
        ["Severity ranking", "Size in metres + clustering → which streets to fix first"],
        ["Construction monitoring", "Satellite 2017 → 2026: new buildings, new roads, water change"],
        ["Edge-ready", "22.6 MB model — runs next to the camera on a drone or inspection vehicle"]];
    return (
        <div className="rh-impact">
            <motion.div {...rise(0)} className="rh-kicker">Guntur drone survey — results</motion.div>
            <div className="rh-nums">
                {nums.map(([n, l, s], i) => (
                    <motion.div key={l} {...rise(1 + i)} className="rh-num">
                        <div className="rh-num-v"><CountUp to={n} suffix={s} /></div>
                        <div className="rh-num-l">{l}</div>
                    </motion.div>
                ))}
            </div>
            <div className="rh-out">
                {out.map(([t, d], i) => (
                    <motion.div key={t} {...rise(5 + i)} className="rh-out-item"><b>{t}</b><span>{d}</span></motion.div>
                ))}
            </div>
        </div>
    );
}

function Outro() {
    return (
        <div className="rh-intro">
            <motion.div {...rise(0)} className="rh-kicker">Smart Property · Urban Monitoring</motion.div>
            <motion.h1 {...rise(1)} className="rh-title">Safer roads, <span>found from the air</span></motion.h1>
            <motion.p {...rise(2)} className="rh-sub">Drone imagery in. Verified, geo-located road defects out — on a live map, ready for the repair crew. The same pipeline extends to bridge decks and tunnel linings.</motion.p>
        </div>
    );
}

export function RoadsHero({ kind }) {
    return { intro: <Intro />, pipeline: <Pipeline />, model: <Model />, impact: <Impact />, outro: <Outro /> }[kind] || null;
}
