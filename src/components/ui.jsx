// Console UI kit — a JSX port of Sentinel's components/ui.tsx, so both
// products are built from the same parts.

/* ── Button ─────────────────────────────────────────────────── */
const BTN_BASE =
    "inline-flex items-center justify-center gap-1.5 rounded-[6px] px-2.5 py-1.5 " +
    "text-[12px] font-medium transition-colors duration-150 disabled:opacity-40 " +
    "disabled:cursor-not-allowed whitespace-nowrap";
const BTN_CLASS = {
    primary: "text-[#04201C] hover:brightness-110",
    ghost: "hover:bg-[var(--surface-2)]",
    danger: "hover:brightness-110",
    subtle: "hover:bg-[var(--surface-2)]",
    active: "",
};
const BTN_STYLE = {
    primary: { background: "var(--signal)" },
    ghost: { color: "var(--text-dim)", border: "1px solid var(--line)" },
    danger: { background: "var(--critical)", color: "#fff" },
    subtle: { color: "var(--text-dim)" },
    active: { color: "var(--signal)", border: "1px solid var(--signal)", background: "var(--signal-dim)" },
};

export function Button({ variant = "ghost", className = "", style, children, ...rest }) {
    return (
        <button
            className={`${BTN_BASE} ${BTN_CLASS[variant]} ${className}`}
            style={{ ...BTN_STYLE[variant], ...style }}
            {...rest}
        >
            {children}
        </button>
    );
}

/* ── Card ───────────────────────────────────────────────────── */
export function Card({ children, className = "", style, ...rest }) {
    return (
        <div
            className={`rounded-[8px] ${className}`}
            style={{ background: "var(--surface)", border: "1px solid var(--line)", boxShadow: "var(--sh-sm)", ...style }}
            {...rest}
        >
            {children}
        </div>
    );
}

/* ── Section header inside rails/panels ─────────────────────── */
export function SectionHeader({ children, right }) {
    return (
        <div className="flex items-center justify-between px-3 py-2">
            <span className="panel-title">{children}</span>
            {right}
        </div>
    );
}

export function Divider() {
    return <div className="mx-3 my-1 h-px" style={{ background: "var(--line)" }} />;
}

/* ── Checkbox mark used by ToggleRow ────────────────────────── */
export function CheckMark({ on, colour, ...rest }) {
    return (
        <span
            className="flex h-[14px] w-[14px] shrink-0 items-center justify-center rounded-[4px] border transition-colors"
            style={{
                borderColor: on ? colour ?? "var(--signal)" : "var(--line)",
                background: on ? colour ?? "var(--signal)" : "transparent",
            }}
            {...rest}
        >
            {on && (
                <svg viewBox="0 0 10 8" className="h-[7px] w-[7px]" fill="none">
                    <path d="M1 4L3.5 6.5L9 1" stroke="#0B1220" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
            )}
        </span>
    );
}

/* ── Toggle row ─────────────────────────────────────────────── */
// `knobProps` lands on the checkbox itself — the guided tour clicks that
// element (data-tour="…-knob"), so it must own the click.
export function ToggleRow({ on, onClick, colour, label, sub, count, icon, tour, knobProps, disabled }) {
    return (
        <div
            data-tour={tour}
            role="button"
            tabIndex={disabled ? -1 : 0}
            aria-pressed={on}
            aria-disabled={disabled}
            onClick={disabled ? undefined : onClick}
            onKeyDown={(e) => { if (!disabled && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onClick?.(); } }}
            className={`group flex w-full select-none items-center gap-2 rounded-[6px] px-2 py-[7px] text-left transition-[background,opacity] duration-150 ${disabled ? "cursor-not-allowed" : "cursor-pointer hover:bg-[var(--surface-2)]"}`}
            style={{ opacity: disabled ? 0.38 : on ? 1 : 0.55 }}
        >
            <CheckMark
                on={on}
                colour={colour}
                {...knobProps}
                onClick={knobProps?.onClick ? (e) => { e.stopPropagation(); if (!disabled) knobProps.onClick(e); } : undefined}
            />
            {icon}
            <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-[12px]" style={{ color: "var(--text)" }} title={label}>{label}</span>
                {sub && <span className="truncate text-[10px]" style={{ color: "var(--text-mute)" }}>{sub}</span>}
            </span>
            {count !== undefined && (
                <span className="mono text-[11px]" style={{ color: "var(--text-mute)" }}>{count}</span>
            )}
        </div>
    );
}

/* ── Switch (pill toggle) ───────────────────────────────────── */
export function Switch({ on }) {
    return (
        <span
            className="h-[14px] w-[24px] shrink-0 rounded-full p-[2px] transition-colors"
            style={{ background: on ? "var(--signal)" : "var(--line)" }}
        >
            <span
                className="block h-[10px] w-[10px] rounded-full bg-white transition-transform"
                style={{ transform: on ? "translateX(10px)" : "none" }}
            />
        </span>
    );
}

/* ── Empty state — written as direction, not mood ───────────── */
export function Empty({ children }) {
    return (
        <div className="px-3 py-6 text-center text-[12px] leading-relaxed" style={{ color: "var(--text-mute)" }}>
            {children}
        </div>
    );
}

/* ── Small pill ─────────────────────────────────────────────── */
export function Pill({ children, colour, mono, className = "" }) {
    return (
        <span
            className={`inline-flex items-center gap-1 rounded-[4px] px-1.5 py-[2px] text-[10px] font-medium ${mono ? "mono" : ""} ${className}`}
            style={{
                background: colour ? `color-mix(in srgb, ${colour} 12%, transparent)` : "var(--surface-2)",
                color: colour ?? "var(--text-dim)",
                border: `1px solid ${colour ? `color-mix(in srgb, ${colour} 27%, transparent)` : "var(--line)"}`,
            }}
        >
            {children}
        </span>
    );
}

/* ── Spinner ────────────────────────────────────────────────── */
export function Spinner({ size = 14 }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" className="animate-spin shrink-0" style={{ color: "var(--signal)" }}>
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.2" />
            <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
        </svg>
    );
}

/* ── Key/value row for metadata cards ───────────────────────── */
export function KV({ k, v, mono }) {
    return (
        <div className="flex items-baseline justify-between gap-3 border-b py-1.5 last:border-0" style={{ borderColor: "var(--line-soft)" }}>
            <dt className="text-[11px]" style={{ color: "var(--text-mute)" }}>{k}</dt>
            <dd className={`truncate text-right text-[12px] ${mono ? "mono" : ""}`} style={{ color: "var(--text)" }}>{v}</dd>
        </div>
    );
}

/* Shared input classes for selects/inputs inside rails and panels. */
export const inputCls =
    "w-full rounded-[6px] px-2.5 py-[6px] text-[12px] outline-none transition-colors " +
    "bg-[var(--surface-2)] border border-[var(--line)] text-[var(--text)] focus:border-[var(--signal)]";
