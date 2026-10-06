import { useEffect, useState } from "react";

// 'narrow' < 760px ≤ 'mid' < 1180px ≤ 'wide'
function read() {
    if (typeof window === "undefined") return "wide";
    const w = window.innerWidth;
    return w < 760 ? "narrow" : w < 1180 ? "mid" : "wide";
}

export function useBreakpoint() {
    const [bp, setBp] = useState(read);
    useEffect(() => {
        const on = () => setBp(read());
        window.addEventListener("resize", on);
        return () => window.removeEventListener("resize", on);
    }, []);
    return bp;
}
