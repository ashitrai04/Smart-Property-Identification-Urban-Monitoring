import { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Cpu } from 'lucide-react';

const BOOT_LINES = [
  '> Initializing GIS Engine v3.2.1',
  '> Loading satellite imagery pipeline …',
  '> Connecting to ArcGIS Feature Services',
  '> Mounting Mapbox GL renderer [OK]',
  '> DeepLabV3+ neural segmentation engine online',
  '> Land Use / Land Cover classification ready',
  '> Building detection model armed',
  '> Property boundary extraction module loaded',
  '> SYSTEM READY — AUTHORIZED PERSONNEL ONLY',
];

export default function BootOverlay({ onComplete }) {
  const [visibleCount, setVisibleCount] = useState(0);
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    let i = 0;
    const interval = setInterval(() => {
      i++;
      setVisibleCount(i);
      setProgress(Math.min((i / BOOT_LINES.length) * 100, 100));
      if (i >= BOOT_LINES.length) {
        clearInterval(interval);
        setTimeout(onComplete, 700);
      }
    }, 280);
    return () => clearInterval(interval);
  }, [onComplete]);

  return (
    <motion.div className="boot-overlay"
      initial={{ opacity: 1 }}
      exit={{ opacity: 0, scale: 1.04 }}
      transition={{ duration: 0.7, ease: 'easeIn' }}
    >
      <div className="boot-grid-bg" />
      <div className="boot-scan-sweep" />

      <div className="boot-content">
        <motion.div className="boot-logo-ring"
          initial={{ scale: 0, rotate: -90, opacity: 0 }}
          animate={{ scale: 1, rotate: 0, opacity: 1 }}
          transition={{ duration: 0.6, ease: 'easeOut' }}
        >
          <svg className="boot-ring-svg" viewBox="0 0 120 120">
            <circle cx="60" cy="60" r="56" fill="none" stroke="rgba(20,184,166,0.15)" strokeWidth="1" />
            <motion.circle cx="60" cy="60" r="56" fill="none" stroke="#14b8a6" strokeWidth="1.5"
              strokeDasharray="8 6" strokeLinecap="round"
              animate={{ rotate: 360 }} transition={{ duration: 12, repeat: Infinity, ease: 'linear' }}
              style={{ transformOrigin: '60px 60px' }}
            />
            <circle cx="60" cy="60" r="44" fill="none" stroke="rgba(20,184,166,0.3)" strokeWidth="0.5" strokeDasharray="2 3" />
          </svg>
          <div className="boot-logo-inner">
            <img src="/yi.png" alt="YI" />
          </div>
        </motion.div>

        <motion.h1 className="boot-title"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3, duration: 0.5 }}
        >
          SMART PROPERTY <span className="boot-title-accent">IDENTIFICATION</span>
        </motion.h1>

        <motion.div className="boot-subtitle"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }}
          transition={{ delay: 0.45 }}
        >
          Urban Monitoring & GIS Intelligence Platform
        </motion.div>

        <div className="boot-terminal">
          <div className="boot-terminal-header">
            <span className="term-dot term-red" />
            <span className="term-dot term-amber" />
            <span className="term-dot term-green" />
            <span className="term-title">SECURE TERMINAL — SMART PROPERTY OPS</span>
          </div>
          <div className="boot-messages">
            {BOOT_LINES.slice(0, visibleCount).map((msg, i) => (
              <motion.div key={i} className="boot-msg"
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.2 }}
              >
                {msg}
              </motion.div>
            ))}
            {visibleCount < BOOT_LINES.length && (
              <div className="boot-cursor">█</div>
            )}
          </div>
        </div>

        <div className="boot-progress">
          <div className="boot-progress-track">
            <motion.div className="boot-progress-fill"
              animate={{ width: `${progress}%` }}
              transition={{ duration: 0.25 }}
            />
          </div>
          <div className="boot-progress-text">
            <Cpu size={10} /> {Math.round(progress)}% · LOADING SYSTEMS
          </div>
        </div>
      </div>
    </motion.div>
  );
}
