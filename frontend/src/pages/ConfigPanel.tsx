import type React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { Button, Spinner, input } from "../ui";

type SaveState = "idle" | "saving" | "saved" | "error";

/* --------------------------------------------------------------------------
 * Schema-driven quick settings
 *
 * The backend exposes a full JSON Schema at /api/config/schema. Rather than
 * build a bespoke form per setting, we render a CURATED allowlist of simple
 * top-level scalar fields (string / integer / boolean) as typed inputs that
 * write straight back into the same JSON document the raw editor owns — so the
 * JSON editor below stays the single source of truth and the fallback for
 * everything the quick form doesn't cover. Nested objects, unions and arrays
 * are intentionally out of scope here.
 * ------------------------------------------------------------------------ */

/** The subset of a JSON-Schema property node the quick form understands. */
interface SchemaProp {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
}

/** A resolved, renderable field: which `[section, key]` it maps to + its schema. */
interface QuickField {
  section: string;
  key: string;
  prop: SchemaProp;
}

/**
 * Curated fields to surface, in render order. Kept deliberately small and
 * hand-picked (all simple scalars) so the form never tries to render something
 * it can't handle cleanly. Each entry is `section.key` into the config object.
 */
const QUICK_FIELDS: { section: string; key: string }[] = [
  { section: "execution", key: "target_branch" },
  { section: "execution", key: "branch_prefix" },
  { section: "execution", key: "auto_merge" },
  { section: "execution", key: "require_full_green" },
  { section: "execution", key: "parallel_reviewers" },
  { section: "execution", key: "review_rounds_max" },
  { section: "execution", key: "wall_clock_budget_minutes" },
];

const SCALAR_TYPES = new Set(["string", "integer", "number", "boolean"]);

/**
 * Resolve the schema property for one `section.key`, following the single
 * `$ref` indirection FastAPI emits for nested models (top-level fields point at
 * an entry in `$defs`). Returns null if anything about the shape is unexpected
 * or the field isn't a simple scalar — the form silently skips such fields.
 */
function resolveProp(
  schema: Record<string, unknown> | null,
  section: string,
  key: string,
): SchemaProp | null {
  if (!schema) return null;
  const defs = (schema.$defs ?? {}) as Record<string, unknown>;
  const topProps = (schema.properties ?? {}) as Record<string, unknown>;
  const sectionNode = topProps[section] as { $ref?: string } | undefined;
  const ref = sectionNode?.$ref;
  if (typeof ref !== "string") return null;
  const defName = ref.split("/").pop();
  if (!defName) return null;
  const def = defs[defName] as { properties?: Record<string, unknown> } | undefined;
  const prop = def?.properties?.[key] as SchemaProp | undefined;
  if (!prop || typeof prop.type !== "string" || !SCALAR_TYPES.has(prop.type)) {
    return null;
  }
  return prop;
}

/** Read `obj[section][key]` defensively (the document may be partial). */
function readValue(
  obj: Record<string, unknown> | null,
  section: string,
  key: string,
): unknown {
  if (!obj) return undefined;
  const sec = obj[section];
  if (sec && typeof sec === "object" && !Array.isArray(sec)) {
    return (sec as Record<string, unknown>)[key];
  }
  return undefined;
}

/** Immutably set `obj[section][key] = value`, creating the section if needed. */
function withValue(
  obj: Record<string, unknown>,
  section: string,
  key: string,
  value: unknown,
): Record<string, unknown> {
  const prevSec = obj[section];
  const sec =
    prevSec && typeof prevSec === "object" && !Array.isArray(prevSec)
      ? (prevSec as Record<string, unknown>)
      : {};
  return { ...obj, [section]: { ...sec, [key]: value } };
}

function prettyLabel(prop: SchemaProp, key: string): string {
  if (prop.title) return prop.title;
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** A single FastAPI-style validation error entry. */
interface FieldError {
  loc?: (string | number)[];
  msg?: string;
  type?: string;
}

function fieldKey(loc: (string | number)[] | undefined): string {
  if (!loc || loc.length === 0) return "(root)";
  // Drop the conventional leading "body" segment for readability.
  const parts = loc[0] === "body" ? loc.slice(1) : loc;
  return parts.length ? parts.join(" › ") : "(root)";
}

/**
 * The api client turns an HTTP error into `Error(message)`, where `message` is
 * either a plain string detail or `JSON.stringify(detail)` (e.g. a 422 with a
 * `detail` array, or a `{detail: ...}` object). Normalize all of those into a
 * human-readable shape so we never dump a raw JSON blob into the UI.
 */
function formatServerError(
  err: unknown,
): { title: string; fields: { key: string; msg: string }[] } {
  const raw = err instanceof Error ? err.message : String(err);

  // Try to recover structured validation detail that was JSON-stringified.
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { title: raw, fields: [] };
  }

  // FastAPI 422: detail is an array of {loc, msg, type}.
  if (Array.isArray(parsed)) {
    const fields = (parsed as FieldError[])
      .map((e) => ({ key: fieldKey(e.loc), msg: e.msg ?? "invalid value" }))
      .filter((f) => f.msg);
    if (fields.length) return { title: "Validation failed", fields };
  }

  // Some handlers wrap as { detail: ... }.
  if (parsed && typeof parsed === "object" && "detail" in parsed) {
    const detail = (parsed as { detail: unknown }).detail;
    if (typeof detail === "string") return { title: detail, fields: [] };
    if (Array.isArray(detail)) {
      const fields = (detail as FieldError[])
        .map((e) => ({ key: fieldKey(e.loc), msg: e.msg ?? "invalid value" }))
        .filter((f) => f.msg);
      if (fields.length) return { title: "Validation failed", fields };
    }
  }

  if (typeof parsed === "string") return { title: parsed, fields: [] };

  // Last resort: a readable single-line summary, not a wall of JSON.
  return { title: raw.slice(0, 300), fields: [] };
}

// The editor fills the remaining viewport height (`h-full`) and scrolls
// INTERNALLY rather than auto-growing the page: no fixed vh height-trap, no
// page/window scroll. `resize-none` because the height is dictated by the
// flex parent, not the user. `whitespace-pre overflow-auto` scrolls long JSON
// (both axes) inside the box instead of overflowing the viewport.
const editorClass =
  "block h-full w-full resize-none overflow-auto " +
  "whitespace-pre rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2.5 " +
  "font-mono text-[13px] leading-relaxed text-zinc-100 outline-none " +
  "focus:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40 " +
  "disabled:opacity-60 disabled:cursor-not-allowed transition-colors";

const DESC_ID = "config-editor-desc";

export function ConfigPanel({ projectId }: { projectId: string }) {
  const [text, setText] = useState("");
  // The last known-good document (from load or successful save) for dirty checks.
  const [baseline, setBaseline] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string>("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [formatted, setFormatted] = useState(false);
  const [errorPanel, setErrorPanel] = useState<
    { title: string; fields: { key: string; msg: string }[] } | null
  >(null);
  // The config JSON Schema (best-effort; the quick form is hidden if it fails).
  const [schema, setSchema] = useState<Record<string, unknown> | null>(null);

  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const loaded = !loading && !loadError;
  const dirty = loaded && text !== baseline;
  const canSave = loaded && saveState !== "saving" && dirty;

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    setErrorPanel(null);
    setSaveState("idle");
    try {
      const cfg = await api.getConfig(projectId);
      const serialized = JSON.stringify(cfg, null, 2);
      setText(serialized);
      setBaseline(serialized);
    } catch (e) {
      // Do NOT clobber the editor; leave it empty and block saving.
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  // Fetch the schema once for the quick-settings form. The schema is global
  // (not per-project), and a failure simply hides the form — the raw JSON
  // editor below is the always-available fallback.
  useEffect(() => {
    let cancelled = false;
    api
      .configSchema()
      .then((s) => {
        if (!cancelled) setSchema(s);
      })
      .catch(() => {
        if (!cancelled) setSchema(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Auto-clear the transient "saved ✓" badge.
  useEffect(() => {
    if (saveState !== "saved") return;
    const id = setTimeout(() => setSaveState("idle"), 2500);
    return () => clearTimeout(id);
  }, [saveState]);

  // Auto-clear the transient "Formatted" confirmation.
  useEffect(() => {
    if (!formatted) return;
    const id = setTimeout(() => setFormatted(false), 1200);
    return () => clearTimeout(id);
  }, [formatted]);

  function onEdit(next: string) {
    setText(next);
    if (saveState !== "idle") setSaveState("idle");
    if (errorPanel) setErrorPanel(null);
  }

  function format() {
    setErrorPanel(null);
    try {
      const pretty = JSON.stringify(JSON.parse(text), null, 2);
      setText(pretty);
      setFormatted(true);
      if (saveState !== "idle") setSaveState("idle");
    } catch {
      setFormatted(false);
      setErrorPanel({
        title: "Cannot format: the document is not valid JSON.",
        fields: [],
      });
    }
  }

  const save = useCallback(async () => {
    if (!loaded) return; // hard guard against blank-overwrite
    setErrorPanel(null);

    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      setSaveState("error");
      setErrorPanel({
        title: "Invalid JSON — fix the syntax before saving.",
        fields: [{ key: "parse", msg: e instanceof Error ? e.message : String(e) }],
      });
      return;
    }

    setSaveState("saving");
    try {
      await api.patchConfig(projectId, parsed);
      // Re-serialize the accepted document as the new baseline.
      const serialized = JSON.stringify(parsed, null, 2);
      setText(serialized);
      setBaseline(serialized);
      setSaveState("saved");
    } catch (e) {
      setSaveState("error");
      setErrorPanel(formatServerError(e));
    }
  }, [loaded, projectId, text]);

  // Best-effort parse of the LIVE editor text into a plain object. While the
  // JSON is mid-edit / invalid this is null and the quick form hides itself,
  // deferring to the raw editor (which shows the syntax error on save).
  const parsedDoc = useMemo<Record<string, unknown> | null>(() => {
    if (!loaded) return null;
    try {
      const v = JSON.parse(text) as unknown;
      return v && typeof v === "object" && !Array.isArray(v)
        ? (v as Record<string, unknown>)
        : null;
    } catch {
      return null;
    }
  }, [loaded, text]);

  // Resolve the curated fields against the fetched schema (skips anything the
  // schema doesn't describe as a simple scalar).
  const quickFields = useMemo<QuickField[]>(() => {
    if (!schema) return [];
    const out: QuickField[] = [];
    for (const { section, key } of QUICK_FIELDS) {
      const prop = resolveProp(schema, section, key);
      if (prop) out.push({ section, key, prop });
    }
    return out;
  }, [schema]);

  // Apply a quick-form edit by rewriting the JSON document, then funnel it
  // through the same onEdit path the raw editor uses (keeps text authoritative,
  // dirty-tracking and save behaviour identical).
  const setQuickField = useCallback(
    (section: string, key: string, value: unknown) => {
      if (!parsedDoc) return;
      const next = withValue(parsedDoc, section, key, value);
      onEdit(JSON.stringify(next, null, 2));
    },
    // onEdit is a stable closure over setState setters; parsedDoc drives this.
    [parsedDoc],
  );

  const showQuickForm = loaded && quickFields.length > 0 && parsedDoc !== null;

  // Ctrl/Cmd+S saves from within the editor (matches editor muscle memory).
  // Scoped to the textarea so it never hijacks the browser's save elsewhere
  // on the single-page layout; only fires when there are changes to persist.
  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (canSave) void save();
    }
  }

  const saveLabel =
    saveState === "saving"
      ? "Saving…"
      : saveState === "saved"
        ? "Saved ✓"
        : "Save config";

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {/* Header — FIXED (the collapsible section title lives in App.tsx) */}
      <div className="flex shrink-0 items-start justify-between gap-3">
        <p id={DESC_ID} className="min-w-0 max-w-2xl text-sm text-zinc-400">
          Edit the raw project config JSON — target branch, per-agent
          models/effort, the green-gate, planning, and more. Changes are
          validated on the server before they are applied. Press{" "}
          <kbd className="rounded border border-zinc-700 bg-zinc-800 px-1 text-[11px] text-zinc-300">
            ⌘/Ctrl+S
          </kbd>{" "}
          to save.
        </p>
        {loaded && dirty && (
          <span
            className="inline-flex shrink-0 items-center gap-1.5 self-center rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-400"
            title="You have unsaved changes in the editor"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-amber-400" aria-hidden="true" />
            Unsaved
          </span>
        )}
      </div>

      {/* Body — fills the remaining viewport height; scrolls INTERNALLY */}
      <div className="min-h-0 flex-1">
        {loading ? (
          <div className="flex h-full min-h-[180px] items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900">
            <span className="flex items-center gap-2 text-sm text-zinc-500">
              <Spinner />
              Loading configuration…
            </span>
          </div>
        ) : loadError ? (
          <div className="h-full space-y-3 overflow-y-auto rounded-lg border border-rose-900/60 bg-rose-950/40 p-4">
            <div className="text-sm font-semibold text-rose-300">
              Couldn’t load the project config
            </div>
            <p className="text-sm text-rose-200/80">
              Saving is disabled to avoid overwriting your real config with an
              empty editor. Retry the load, then edit.
            </p>
            <p className="font-mono text-xs break-words text-rose-300/70 select-text">
              {loadError}
            </p>
            <Button variant="ghost" onClick={load} title="Reload the config from the server">
              Retry load
            </Button>
          </div>
        ) : (
          <div className="flex h-full min-h-0 flex-col gap-3">
            {showQuickForm && (
              <QuickSettings
                fields={quickFields}
                doc={parsedDoc}
                onChange={setQuickField}
              />
            )}
            {/* Raw JSON editor — fills the remaining space and scrolls
                internally. It stays the source of truth and the fallback for
                everything the quick form above doesn't cover. */}
            <textarea
              ref={taRef}
              className={`${editorClass} min-h-[160px] flex-1`}
              spellCheck={false}
              autoCorrect="off"
              autoCapitalize="off"
              autoComplete="off"
              wrap="off"
              value={text}
              onChange={(e) => onEdit(e.target.value)}
              onKeyDown={onKeyDown}
              aria-label="Project configuration JSON editor"
              aria-describedby={DESC_ID}
            />
          </div>
        )}
      </div>

      {/* Server / client error panel (never a raw JSON blob) — FIXED, but
          caps its own height and scrolls internally if many fields fail. */}
      {errorPanel && (
        <div
          role="alert"
          aria-live="polite"
          className="max-h-[30vh] shrink-0 overflow-y-auto rounded-lg border border-rose-900/60 bg-rose-950/40 p-3"
        >
          <div className="text-sm font-semibold text-rose-300">
            {errorPanel.title}
          </div>
          {errorPanel.fields.length > 0 && (
            <ul className="mt-2 space-y-1">
              {errorPanel.fields.map((f, i) => (
                <li key={i} className="flex gap-2 text-xs text-rose-200/90">
                  <code className="shrink-0 font-mono text-rose-300/80">
                    {f.key}
                  </code>
                  <span className="text-rose-300/50">—</span>
                  <span className="min-w-0 break-words">{f.msg}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Actions — FIXED; wrap on narrow screens so nothing is pushed off-canvas */}
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        <Button
          variant="primary"
          onClick={() => void save()}
          disabled={!canSave}
          title={dirty ? "Save changes (⌘/Ctrl+S)" : "No changes to save"}
        >
          {saveLabel}
        </Button>
        <Button
          variant="ghost"
          onClick={format}
          disabled={!loaded}
          title="Re-indent and normalize the JSON"
        >
          Format
        </Button>
        {formatted && (
          <span className="text-xs text-indigo-400" aria-live="polite">
            Formatted
          </span>
        )}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Quick-settings form (schema-driven, curated scalar fields)
 * ------------------------------------------------------------------------ */

/**
 * Collapsible block of typed inputs for the curated config fields. Defaults to
 * OPEN so the affordance is discoverable, but caps its own height and scrolls
 * internally so it never crowds out the raw JSON editor below. Every edit flows
 * back through `onChange`, which rewrites the shared JSON document.
 */
function QuickSettings({
  fields,
  doc,
  onChange,
}: {
  fields: QuickField[];
  doc: Record<string, unknown>;
  onChange: (section: string, key: string, value: unknown) => void;
}) {
  const [open, setOpen] = useState(true);

  return (
    <section className="shrink-0 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex min-h-[44px] w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-zinc-800/50 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
      >
        <svg
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
          className={`h-4 w-4 shrink-0 text-zinc-500 transition-transform duration-200 ${
            open ? "rotate-90" : ""
          }`}
        >
          <path
            fillRule="evenodd"
            d="M7.21 14.77a.75.75 0 0 1 .02-1.06L11.168 10 7.23 6.29a.75.75 0 1 1 1.04-1.08l4.5 4.25a.75.75 0 0 1 0 1.08l-4.5 4.25a.75.75 0 0 1-1.06-.02Z"
            clipRule="evenodd"
          />
        </svg>
        <span className="flex-1 text-sm font-semibold tracking-wide text-zinc-200">
          Quick settings
        </span>
        <span className="shrink-0 text-xs text-zinc-500">common execution options</span>
      </button>
      {open && (
        <div className="max-h-[40vh] space-y-3 overflow-y-auto border-t border-zinc-800 p-3">
          {fields.map((f) => (
            <QuickFieldRow
              key={`${f.section}.${f.key}`}
              field={f}
              value={readValue(doc, f.section, f.key)}
              onChange={(v) => onChange(f.section, f.key, v)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

/** One labelled input, rendered by JSON-schema type. */
function QuickFieldRow({
  field,
  value,
  onChange,
}: {
  field: QuickField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const { prop, key } = field;
  const label = prettyLabel(prop, key);
  const id = `cfg-${field.section}-${key}`;
  const descId = prop.description ? `${id}-desc` : undefined;

  // Booleans render as a single clickable row (checkbox + label).
  if (prop.type === "boolean") {
    return (
      <label
        htmlFor={id}
        className="flex cursor-pointer items-start gap-2.5 text-sm text-zinc-200"
      >
        <input
          id={id}
          type="checkbox"
          className="mt-0.5 h-4 w-4 shrink-0 accent-indigo-500"
          checked={value === true}
          aria-describedby={descId}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="min-w-0">
          <span className="font-medium">{label}</span>
          {prop.description && (
            <span id={descId} className="mt-0.5 block text-xs text-zinc-500">
              {prop.description}
            </span>
          )}
        </span>
      </label>
    );
  }

  const isNumber = prop.type === "integer" || prop.type === "number";

  return (
    <label htmlFor={id} className="block">
      <span className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-zinc-300">{label}</span>
        {isNumber && (prop.minimum !== undefined || prop.maximum !== undefined) && (
          <span className="text-[11px] tabular-nums text-zinc-600">
            {prop.minimum ?? "−∞"}–{prop.maximum ?? "∞"}
          </span>
        )}
      </span>
      <input
        id={id}
        className={input}
        type={isNumber ? "number" : "text"}
        min={isNumber ? prop.minimum : undefined}
        max={isNumber ? prop.maximum : undefined}
        step={prop.type === "integer" ? 1 : undefined}
        value={value === undefined || value === null ? "" : String(value)}
        aria-describedby={descId}
        onChange={(e) => {
          if (isNumber) {
            const raw = e.target.value;
            // Empty clears back to the schema default rather than writing NaN.
            if (raw === "") {
              onChange(prop.default ?? undefined);
              return;
            }
            const n = Number(raw);
            onChange(Number.isFinite(n) ? n : raw);
          } else {
            onChange(e.target.value);
          }
        }}
      />
      {prop.description && (
        <span id={descId} className="mt-1 block text-xs text-zinc-500">
          {prop.description}
        </span>
      )}
    </label>
  );
}
