// The API root, on its own so a component that only needs this string does not
// import mapLayers.js — which imports mapbox-gl, and pulled ~800 KB into the
// entry chunk just because the top bar wanted a URL.
export const API_BASE = (import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000").replace(/\/+$/, "");
