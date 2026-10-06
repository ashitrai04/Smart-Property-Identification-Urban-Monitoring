import { useEffect, useState } from "react";
import { motion } from "framer-motion";

/* Each line is a real subsystem of the platform, not flavour text. */
const BOOT_LINES = [
    ["Mapbox GL renderer", "OK"],
    ["District vector store · 5 districts", "OK"],
    ["SegFormer-B5 land-use segmentation", "ARMED"],
    ["SAM2 building-footprint refinement", "ARMED"],
    ["Change detection · building / vegetation", "READY"],
    ["AOI analytics engine", "READY"],
    ["Activity log · Supabase", "LINKED"],
];
const STEP_MS = 150;

export default function BootOverlay({ onComplete }) {
    const [count, setCount] = useState(0);
    const progress = Math.round((count / BOOT_LINES.length) * 100);

    useEffect(() => {
        let i = 0;
        const iv = setInterval(() => {
            i++;
            setCount(i);
            if (i >= BOOT_LINES.length) {
                clearInterval(iv);
                setTimeout(onComplete, 420);
            }
        }, STEP_MS);
        return () => clearInterval(iv);
    }, [onComplete]);

    return (
        <motion.div
            className="boot-overlay"
            initial={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
        >
            <div className="boot-grid-bg" />

            <motion.div
                className="boot-content"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8, scale: 0.985 }}
                transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
            >
                <div className="boot-mark">
                    <img src="/yi.png" alt="" />
                    <div>
                        <div className="boot-title">Smart Property</div>
                        <div className="boot-subtitle">Urban monitoring · Andhra Pradesh</div>
                    </div>
                </div>

                <div className="boot-terminal">
                    <div className="boot-terminal-header">
                        <span className="panel-title">System check</span>
                        <span className="mono text-[10px]" style={{ color: "var(--text-mute)" }}>
                            {count}/{BOOT_LINES.length}
                        </span>
                    </div>
                    <div className="boot-messages">
                        {BOOT_LINES.slice(0, count).map(([label, state]) => (
                            <motion.div
                                key={label}
                                className="boot-msg"
                                initial={{ opacity: 0, x: -6 }}
                                animate={{ opacity: 1, x: 0 }}
                                transition={{ duration: 0.18 }}
                            >
                                <span className="truncate">{label}</span>
                                <span className="ok">{state}</span>
                            </motion.div>
                        ))}
                        {count < BOOT_LINES.length && <div className="boot-cursor">▍</div>}
                    </div>
                </div>

                <div>
                    <div className="boot-progress-track">
                        <motion.div
                            className="boot-progress-fill"
                            initial={{ width: 0 }}
                            animate={{ width: `${progress}%` }}
                            transition={{ duration: STEP_MS / 1000, ease: "linear" }}
                        />
                    </div>
                    <div className="boot-progress-text">
                        <span>{progress === 100 ? "Ready" : "Initialising console"}</span>
                        <span>{progress}%</span>
                    </div>
                </div>
            </motion.div>
        </motion.div>
    );
}
