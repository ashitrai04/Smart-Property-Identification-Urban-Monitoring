// Tiny action bus so the guided tour can drive actions that live inside page
// components (which own their own state) without tight coupling. Pages register
// an API under a scope on mount; the tour calls it, waiting for the page to mount.

const handlers = {};

export function registerTour(scope, api) {
    handlers[scope] = { ...(handlers[scope] || {}), ...api };
}
export function unregisterTour(scope) {
    delete handlers[scope];
}
export function hasTour(scope, fn) {
    return !!(handlers[scope] && typeof handlers[scope][fn] === "function");
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Call a registered action, waiting up to ~9s for the page to register it.
export async function callTour(scope, fn, ...args) {
    for (let i = 0; i < 60; i++) {
        if (hasTour(scope, fn)) {
            try { return await handlers[scope][fn](...args); }
            catch (e) { console.warn("tour action failed:", scope, fn, e); return; }
        }
        await sleep(150);
    }
    console.warn("tour action unavailable:", scope, fn);
}

// Wait for a DOM element (by selector) to exist.
export async function waitForEl(sel, timeout = 8000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
        const el = document.querySelector(sel);
        if (el) return el;
        await sleep(120);
    }
    return null;
}

// Fetch a public asset and wrap it as a File (for simulated uploads).
export async function fetchFile(url, name, type) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`fetch ${url} -> ${r.status}`);
    const b = await r.blob();
    return new File([b], name, { type: type || b.type || "application/octet-stream" });
}
