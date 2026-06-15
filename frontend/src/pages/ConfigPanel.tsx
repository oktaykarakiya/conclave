import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api";
import { Button, Spinner } from "../ui";

type SaveState = "idle" | "saving" | "saved" | "error";

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
          <textarea
            ref={taRef}
            className={editorClass}
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
