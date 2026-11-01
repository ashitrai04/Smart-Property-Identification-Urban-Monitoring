import { gpuBase, probeGpu } from "./modelApi";

/*
 * Building heights, from the DSM laptop.
 *
 * The GPU server detects footprints from imagery; a second machine holds the
 * DSM and DTM and measures how tall each one is. That machine is usually off,
 * so every call here is written to fail quietly: a missing height is a missing
 * column, never a failed detection. Callers do not have to check anything
 * before asking.
 *
 * Both places that show buildings use this, so the field names, the batching
 * and the "is it even there" check exist once.
 */

// The fields the height server adds. Named here so a merge is explicit rather
// than a spread of whatever the server happened to send.
export const HEIGHT_FIELDS = [
    "height_m", "mean_height_m", "max_height_m", "min_height_m",
    "floors", "floor_label", "ground_elev_m", "roof_elev_m",
    "height_source", "dtm_minus_ring_m", "n_pixels", "gsd_m",
    "roughness_m", "quality",
];

// Footprints per request. Small enough that a batch comes back while the next
// detection window is still running, so heights appear beside buildings rather
// than after them.
const CHUNK = 300;
const PROBE_TTL = 60_000;

/**
 * fetch() against the height endpoints.
 *
 * Deliberately NOT gpuOnlyFetch. That helper marks the whole GPU server down
 * for a minute whenever a call fails, and the detection polling reads the same
 * flag - so one height hiccup made every later call, detection included,
 * report GPU_OFFLINE. Heights are an enrichment; they are not allowed to take
 * the model server offline on everyone else's behalf.
 *
 * probeGpu() is still awaited because it is what discovers the server's
 * current address, but its verdict is not treated as fatal here: the address
 * is what is wanted, and the request itself decides whether heights work.
 */
export async function heightFetch(path, init = {}) {
    await probeGpu();
    const base = gpuBase();
    if (!base) {
        const e = new Error("no model server address is known yet");
        e.quiet = true;
        throw e;
    }
    return fetch(base + path, init);
}

/** Turn whatever went wrong into something worth showing a person. */
function readable(e) {
    const m = String(e?.message || e || "");
    if (m === "GPU_OFFLINE") return "the model server is offline";
    if (m === "Failed to fetch" || e?.name === "TypeError") {
        return "the model server could not be reached";
    }
    return m || "the height server did not answer";
}

let probe = { at: 0, ok: false, reason: "not checked yet", info: null };
let inflight = null;

/** Is the height server reachable? Cached for a minute. Never throws. */
export async function heightsAvailable(force = false) {
    if (!force && Date.now() - probe.at < PROBE_TTL) return probe;
    if (inflight) return inflight;
    inflight = (async () => {
        try {
            const r = await heightFetch("/height/health");
            if (r.status === 404) {
                // The data server proxies model calls; a 404 here means it has
                // no /height route, not that the DSM machine is down.
                probe = { at: Date.now(), ok: false, info: null,
                          reason: "the model server has no height route — restart it" };
                return probe;
            }
            const d = await r.json();
            probe = {
                at: Date.now(), ok: !!d.ok, info: d,
                reason: d.ok ? "" : (d.reason || "the height server is not reachable"),
            };
        } catch (e) {
            probe = { at: Date.now(), ok: false, info: null, reason: readable(e) };
        } finally {
            inflight = null;
        }
        return probe;
    })();
    return inflight;
}

export function lastHeightProbe() {
    return probe;
}

/**
 * Measure a list of EPSG:4326 GeoJSON features.
 *
 * Returns an array the same length and order as the input, each entry the
 * height properties for that feature or null if it could not be measured.
 * Returns null for the whole call when there is no height server.
 */
export async function measureFeatures(features, { signal } = {}) {
    if (!features?.length) return null;
    const out = new Array(features.length).fill(null);
    let got = 0;

    for (let i = 0; i < features.length; i += CHUNK) {
        const slice = features.slice(i, i + CHUNK);
        let d;
        try {
            const r = await heightFetch("/height/enrich", {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({
                    type: "FeatureCollection",
                    // Geometry only: the server echoes properties back, and
                    // sending detection metadata it has no use for would just
                    // make the request bigger.
                    features: slice.map((f) => ({
                        type: "Feature", geometry: f.geometry, properties: {},
                    })),
                }),
                signal,
            });
            d = await r.json();
            if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        } catch (e) {
            if (e.name === "AbortError") throw e;
            probe = { at: Date.now(), ok: false, info: null, reason: readable(e) };
            break;    // the laptop went away mid-run; keep what already arrived
        }

        const back = d.features || [];
        // Order is the only join key. A length mismatch makes the batch
        // unusable, and guessing an alignment would put heights on the wrong
        // buildings without anything looking wrong.
        if (back.length !== slice.length) break;
        if (d.height_status && d.height_status.state === "not_configured") {
            probe = { at: Date.now(), ok: false, info: null,
                      reason: d.height_status.detail || "no height server configured" };
            break;
        }
        for (let k = 0; k < back.length; k++) {
            const p = back[k].properties || {};
            if (p.height_m == null && !p.quality) continue;
            const picked = {};
            for (const f of HEIGHT_FIELDS) if (f in p) picked[f] = p[f];
            out[i + k] = picked;
            got++;
        }
    }
    return got ? out : null;
}

/** 0 when there is no height, else 1-6 where 6 means "six or more". */
export function floorBin(floors) {
    if (floors == null || !Number.isFinite(floors)) return 0;
    return Math.max(1, Math.min(6, Math.round(floors)));
}

/**
 * Roll up whatever has been measured so far.
 *
 * Computed here from the features on screen rather than taken from the
 * server's own per-request summary, because during a live run the summary
 * would only describe the latest batch — and a running total that disagrees
 * with the map is worse than no total.
 */
export function summarise(propsList) {
    const hs = [];
    const bins = [0, 0, 0, 0, 0, 0];
    const quality = {};
    let measured = 0;
    for (const p of propsList) {
        if (!p) continue;
        if (p.quality) quality[p.quality] = (quality[p.quality] || 0) + 1;
        if (p.height_m == null) continue;
        measured++;
        hs.push(p.height_m);
        const b = floorBin(p.floors);
        if (b) bins[b - 1]++;
    }
    if (!measured) return { measured: 0, n: propsList.length, bins, quality };
    hs.sort((a, b) => a - b);
    return {
        measured,
        n: propsList.length,
        bins,
        quality,
        mean_height_m: Math.round((hs.reduce((a, b) => a + b, 0) / hs.length) * 100) / 100,
        median_height_m: hs[Math.floor(hs.length / 2)],
        max_height_m: hs[hs.length - 1],
    };
}
