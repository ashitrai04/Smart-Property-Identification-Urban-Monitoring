/*
 * Carry one result from the Analysis page to the map.
 *
 * A georeferenced upload (a GeoTIFF) comes back with world coordinates, so it
 * belongs on the map rather than only in the panel's own preview. The two
 * pages are separate routes with no shared store, and the payload is a single
 * throwaway FeatureCollection, so sessionStorage is the right size of
 * mechanism: it survives the navigation, dies with the tab, and needs no
 * provider threaded through the tree.
 *
 * take() is destructive on purpose — handing the same result over twice would
 * put it back on the map every time the user returned to the page.
 */
const KEY = "urbanly.map.handoff";

export function sendToMap(payload) {
    try {
        sessionStorage.setItem(KEY, JSON.stringify({ ...payload, at: Date.now() }));
        return true;
    } catch {
        return false;   // private mode, or the result is larger than the quota
    }
}

export function takeFromMap(maxAgeMs = 10 * 60 * 1000) {
    try {
        const raw = sessionStorage.getItem(KEY);
        if (!raw) return null;
        sessionStorage.removeItem(KEY);
        const d = JSON.parse(raw);
        if (!d?.geojson?.features?.length) return null;
        // Stale enough that the user has moved on: showing it would be noise.
        if (maxAgeMs && Date.now() - (d.at || 0) > maxAgeMs) return null;
        return d;
    } catch {
        return null;
    }
}
