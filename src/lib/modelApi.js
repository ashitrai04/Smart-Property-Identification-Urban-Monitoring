import { useSyncExternalStore } from "react";
import { API_BASE } from "../utils/apiBase";

/*
 * Where the AI models run.
 *
 * Primary:  our A100 GPU server (gpu-server/), reached through a Cloudflare tunnel.
 *           The free tunnel gets a new address on every restart, so the server
 *           publishes it and the data backend serves it at /api/gpu-server —
 *           the webapp looks it up here and never needs a redeploy for it.
 *           VITE_GPU_API, if set, is only a fallback for that lookup.
 * Fallback: the SegFormer Hugging Face Space (CPU), same endpoints and responses.
 *
 * modelFetch(path) tries the GPU server first and quietly falls back to Hugging
 * Face when it is unreachable, errors (5xx) or reports it is out of GPU memory
 * (503). Both use the same R2 bucket, so a large upload's key works on either.
 */
export const HF_MODEL_BASE = "https://asashit-smart-property-segformer.hf.space";
const ENV_GPU = (import.meta.env.VITE_GPU_API || "").replace(/\/+$/, "");

const RECHECK_MS = 30_000;   // re-probe a healthy server this often
const DOWN_MS = 60_000;      // after a failure, skip the GPU server this long
const DISCOVER_MS = 60_000;  // re-ask the backend for the current tunnel address

let state = { base: ENV_GPU, ok: false, checked: 0, info: null, downUntil: 0, discovered: 0 };
const listeners = new Set();
const emit = () => listeners.forEach((l) => l());
let probing = null;
let discovering = null;

function set(next) {
    state = { ...state, ...next };
    emit();
}

/** Ask the data backend for the GPU server's current public address. */
function discover(force = false) {
    if (!force && Date.now() - state.discovered < DISCOVER_MS) return Promise.resolve(state.base);
    if (discovering) return discovering;
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 5000);
    discovering = fetch(`${API_BASE}/api/gpu-server`, { cache: "no-store", signal: ctl.signal })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null)
        .then((d) => {
            const url = d?.online && d?.url ? String(d.url).replace(/\/+$/, "") : "";
            const base = url || ENV_GPU;
            // A new address means a restarted server: forget the old health verdict.
            if (base !== state.base) set({ base, ok: false, checked: 0, downUntil: 0, discovered: Date.now() });
            else set({ discovered: Date.now() });
            return base;
        })
        .finally(() => { clearTimeout(t); discovering = null; });
    return discovering;
}

/** Is the GPU server reachable right now? Cached; pass force to re-check. */
/** One health probe. Resolves to the server's info, or null. */
function ping(base, ms = 4000) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms);
    return fetch(`${base}/api/health`, { cache: "no-store", signal: ctl.signal })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null)
        .finally(() => clearTimeout(t));
}

/*
 * Addresses to try when the published one does not answer.
 *
 * The published address is a Cloudflare quick tunnel, and a freshly created
 * hostname is not resolvable everywhere at once - a phone hotspot's resolver
 * returned "non-existent domain" for one that 1.1.1.1 was already serving.
 * When that happens and the browser is on the same machine as the server,
 * localhost is sitting right there and works.
 *
 * Chrome treats http://127.0.0.1 as a trustworthy origin, so this is not
 * blocked as mixed content from the deployed HTTPS site.
 */
const LOCAL_FALLBACKS = ["http://127.0.0.1:8777", "http://127.0.0.1:8800"];

export async function probeGpu(force = false) {
    const base = await discover(force);
    const now = Date.now();
    if (!force && now < state.downUntil) return false;
    if (!force && state.checked && now - state.checked < RECHECK_MS) return state.ok;
    if (probing) return probing;

    probing = (async () => {
        if (base) {
            const info = await ping(base);
            if (info && info.status === "ok") {
                set({ ok: true, info, checked: Date.now(), downUntil: 0 });
                return true;
            }
        }
        // The published address is unreachable from here. Before declaring the
        // whole stack offline, check whether it is simply running on this
        // machine - which it is whenever someone is working at the server.
        for (const alt of LOCAL_FALLBACKS) {
            if (alt === base) continue;
            const info = await ping(alt, 1500);
            if (info && info.status === "ok") {
                set({ base: alt, ok: true, info, checked: Date.now(), downUntil: 0 });
                return true;
            }
        }
        set({ ok: false, checked: Date.now(), downUntil: Date.now() + DOWN_MS });
        return false;
    })().finally(() => { probing = null; });

    return probing;
}

function markDown() {
    set({ ok: false, checked: Date.now(), downUntil: Date.now() + DOWN_MS });
}

/** fetch() against the model API: GPU server first, Hugging Face fallback. */
export async function modelFetch(path, init = {}) {
    if (await probeGpu()) {
        try {
            const r = await fetch(state.base + path, init);
            if (r.status < 500) return r;          // success, or a real client error worth showing
            // 502/503/504 = tunnel down or GPU out of memory: skip the GPU server for a while.
            // Any other 5xx is specific to this call (e.g. R2 not configured there) —
            // fall back for this request only and keep using the GPU for the rest.
            if (r.status >= 502) markDown();
        } catch {
            markDown();                            // network error / tunnel down -> fall back
        }
    }
    return fetch(HF_MODEL_BASE + path, init);
}

/*
 * Some models only exist on the GPU server. Building delineation (HiSup) is
 * one: it is a 1.2 GB checkpoint doing multi-scale inference, which the CPU
 * Hugging Face Space cannot carry. Falling back for it would silently hit a
 * 404 on the Space and look like a bug, so these calls fail honestly instead.
 */
export async function gpuOnlyFetch(path, init = {}) {
    if (!(await probeGpu())) {
        throw new Error("GPU_OFFLINE");
    }
    try {
        const r = await fetch(state.base + path, init);
        if (r.status >= 502) markDown();
        return r;
    } catch (e) {
        markDown();
        throw e;
    }
}

/** Does the GPU server advertise this capability right now? */
export function hasCapability(name) {
    const caps = state.info?.capabilities;
    return Array.isArray(caps) && caps.includes(name);
}

/** Current GPU server base URL ("" if none known). */
export function gpuBase() {
    return state.base;
}

/** The base URL currently serving models (for links / GPU-only features). */
export function activeModelBase() {
    return state.ok ? state.base : HF_MODEL_BASE;
}

/** React hook: { base, ok, info } for the GPU server, live. */
export function useGpuStatus() {
    return useSyncExternalStore(
        (l) => { listeners.add(l); return () => listeners.delete(l); },
        () => state,
    );
}
