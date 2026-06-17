// Shared UI primitives + design tokens used across multiple Conclave pages.
// Extracted from the original monolithic panels.tsx so each page file owns only
// its own helpers and imports the shared bits from here.

import type React from "react";

// --- design tokens ----------------------------------------------------------

export const STATE_COLORS: Record<string, string> = {
  inbox: "bg-zinc-600",
  approved: "bg-indigo-600",
  in_progress: "bg-amber-500",
  done: "bg-emerald-600",
  failed: "bg-rose-600",
  blocked: "bg-orange-600",
  cancelled: "bg-zinc-500",
};

export const VERDICT_COLORS: Record<string, string> = {
  pass: "text-emerald-400",
  fail: "text-rose-400",
  block: "text-rose-400",
  decline: "text-amber-400",
  unknown: "text-zinc-400",
};

/** Shared text-input class (indigo accent, per the design brief). */
export const input =
  "w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-500 focus:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40";

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

// --- primitives -------------------------------------------------------------

export function Badge({ text, color }: { text: string; color?: string }) {
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-medium text-white ${color ?? "bg-zinc-600"}`}
    >
      {text}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
  title,
}: {
  children: React.ReactNode;
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
}) {
  const styles: Record<string, string> = {
    default: "bg-zinc-700 hover:bg-zinc-600 text-zinc-100",
    primary: "bg-indigo-600 hover:bg-indigo-500 text-white",
    danger: "bg-red-700 hover:bg-red-600 text-white",
    ghost: "bg-transparent hover:bg-zinc-800 text-zinc-300",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`rounded px-3 py-1.5 text-sm font-medium transition focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none disabled:opacity-40 ${styles[variant]}`}
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
    <div className={`rounded-xl border border-zinc-800 bg-zinc-900 p-4 ${className}`}>
      {children}
    </div>
  );
}
