import { useSyncExternalStore } from "react";

// Shared map preferences: the base-map switcher lives in the command bar,
// the map that uses it lives in the Mapping page.

export const BASE_MAPS = [
    { id: "dark-v11", label: "Dark" },
    { id: "satellite-streets-v12", label: "Satellite" },
    { id: "streets-v12", label: "Streets" },
    { id: "light-v11", label: "Light" },
    { id: "outdoors-v12", label: "Terrain" },
];

const KEY = "sp-basemap";
const listeners = new Set();
let baseMap = (() => {
    try {
        const v = localStorage.getItem(KEY);
        if (BASE_MAPS.some((b) => b.id === v)) return v;
    } catch { /* storage unavailable */ }
    return "satellite-streets-v12";
})();

export function getBaseMap() {
    return baseMap;
}

export function setBaseMap(id) {
    if (id === baseMap || !BASE_MAPS.some((b) => b.id === id)) return;
    baseMap = id;
    try { localStorage.setItem(KEY, id); } catch { /* ignore */ }
    listeners.forEach((l) => l());
}

function subscribe(l) {
    listeners.add(l);
    return () => listeners.delete(l);
}

export function useBaseMap() {
    return [useSyncExternalStore(subscribe, getBaseMap), setBaseMap];
}
