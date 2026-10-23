// Shared minimalist UI primitives + design tokens used across Conclave views.

import type React from "react";

// --- design tokens ----------------------------------------------------------

export const STATE_COLORS: Record<string, string> = {
  inbox: "bg-zinc-800 text-zinc-300 border border-zinc-700",
  approved: "bg-indigo-950/70 text-indigo-300 border border-indigo-800/70",
  in_progress: "bg-amber-950/70 text-amber-300 border border-amber-700/70",
  waiting_for_approval: "bg-sky-950/80 text-sky-300 border border-sky-700/80",
  done: "bg-emerald-950/70 text-emerald-300 border border-emerald-800/70",
  failed: "bg-rose-950/70 text-rose-300 border border-rose-800/70",
  blocked: "bg-orange-950/70 text-orange-300 border border-orange-800/70",
  cancelled: "bg-zinc-900 text-zinc-500 border border-zinc-800",
};

export const STATE_DOTS: Record<string, string> = {
  inbox: "bg-zinc-500",
  approved: "bg-indigo-400",
  in_progress: "bg-amber-400 animate-pulse",
  waiting_for_approval: "bg-sky-400 animate-ping",
  done: "bg-emerald-400",
  failed: "bg-rose-400",
  blocked: "bg-orange-400",
  cancelled: "bg-zinc-600",
};

export const VERDICT_COLORS: Record<string, string> = {
  pass: "text-emerald-400",
  fail: "text-rose-400",
  block: "text-rose-400",
  decline: "text-amber-400",
  unknown: "text-zinc-400",
};

export const NODE_KIND_COLORS: Record<string, { border: string; badge: string; dot: string }> = {
  planner: {
    border: "border-sky-500/50",
    badge: "bg-sky-950/80 text-sky-300 border-sky-800/60",
    dot: "bg-sky-400",
  },
  developer: {
    border: "border-indigo-500/60",
    badge: "bg-indigo-950/80 text-indigo-300 border-indigo-800/60",
    dot: "bg-indigo-400",
  },
  reviewer: {
    border: "border-violet-500/50",
    badge: "bg-violet-950/80 text-violet-300 border-violet-800/60",
    dot: "bg-violet-400",
  },
  gate: {
    border: "border-emerald-500/50",
    badge: "bg-emerald-950/80 text-emerald-300 border-emerald-800/60",
    dot: "bg-emerald-400",
  },
  merge: {
    border: "border-teal-500/50",
    badge: "bg-teal-950/80 text-teal-300 border-teal-800/60",
    dot: "bg-teal-400",
  },
  custom: {
    border: "border-amber-500/50",
    badge: "bg-amber-950/80 text-amber-300 border-amber-800/60",
    dot: "bg-amber-400",
  },
};

/** Shared minimalist text-input class. */
export const input =
  "w-full rounded-lg border border-zinc-800 bg-zinc-900/90 px-3 py-1.5 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-500 focus:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40";

// --- formatting helpers -----------------------------------------------------

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtRelative(iso: string): string {
  const diff = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// --- primitives -------------------------------------------------------------

export function Badge({ text, color }: { text: string; color?: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-medium tracking-tight ${
        color ?? "bg-zinc-800 text-zinc-300 border border-zinc-700"
      }`}
    >
      {text}
    </span>
  );
}

export function StatusDot({ state }: { state: string }) {
  const dot = STATE_DOTS[state] ?? "bg-zinc-500";
  return <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${dot}`} />;
}

export function Button({
  children,
  onClick,
  variant = "default",
  size = "md",
  disabled,
  type = "button",
  title,
  className = "",
}: {
  children: React.ReactNode;
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "sm" | "md";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
  className?: string;
}) {
  const styles: Record<string, string> = {
    default: "bg-zinc-800/90 hover:bg-zinc-700 text-zinc-100 border border-zinc-700/80",
    primary: "bg-indigo-600 hover:bg-indigo-500 text-white border border-indigo-500/40",
    danger: "bg-rose-950/80 hover:bg-rose-900 text-rose-200 border border-rose-800/70",
    ghost: "bg-transparent hover:bg-zinc-800/70 text-zinc-400 hover:text-zinc-200",
  };
  const sizes: Record<string, string> = {
    sm: "px-2.5 py-1 text-xs",
    md: "px-3 py-1.5 text-xs",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none disabled:opacity-40 ${sizes[size]} ${styles[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

/** Small inline loading spinner (zinc track, indigo head). Tailwind-only. */
export function Spinner({ size = 16, className = "" }: { size?: number; className?: string }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      style={{ width: size, height: size, borderWidth: Math.max(2, Math.round(size / 8)) }}
      className={`inline-block animate-spin rounded-full border-solid border-zinc-700 border-t-indigo-400 align-[-0.125em] ${className}`}
    />
  );
}

/** Simple rounded surface used to wrap content blocks. */
export function Card({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-xl border border-zinc-800/80 bg-zinc-900/60 p-4 ${className}`}>
      {children}
    </div>
  );
}
