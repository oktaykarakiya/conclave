import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { Button } from "../ui";

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

const editorClass =
  "w-full h-[58vh] resize-y rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2.5 " +
  "font-mono text-[13px] leading-relaxed text-zinc-100 outline-none " +
  "focus:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40 " +
  "disabled:opacity-60 disabled:cursor-not-allowed transition-colors";

export function ConfigPanel({ projectId }: { projectId: string }) {
  const [text, setText] = useState("");
  // The last known-good document (from load or successful save) for dirty checks.
  const [baseline, setBaseline] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string>("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [errorPanel, setErrorPanel] = useState<
    { title: string; fields: { key: string; msg: string }[] } | null
  >(null);

  const loaded = !loading && !loadError;
  const dirty = loaded && text !== baseline;

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
      if (saveState !== "idle") setSaveState("idle");
    } catch {
      setErrorPanel({
        title: "Cannot format: the document is not valid JSON.",
        fields: [],
      });
    }
  }

  async function save() {
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
  }

  const saveLabel =
    saveState === "saving"
      ? "Saving…"
      : saveState === "saved"
        ? "Saved ✓"
        : "Save config";

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
            Project configuration
          </h3>
          <p className="mt-1 max-w-2xl text-sm text-zinc-400">
            Edit the raw project config JSON — target branch, per-agent
            models/effort, the green-gate, planning, and more. Changes are
            validated on the server before they are applied.
          </p>
        </div>
        {loaded && (
          <span
            className="shrink-0 self-center text-xs tabular-nums text-zinc-500"
            title="Unsaved changes are present in the editor"
          >
            {dirty ? "Unsaved changes" : "Up to date"}
          </span>
        )}
      </div>

      {/* Body: loading / load-error / editor */}
      {loading ? (
        <div className="flex h-[58vh] items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900">
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-zinc-600 border-t-zinc-300" />
            Loading configuration…
          </div>
        </div>
      ) : loadError ? (
        <div className="space-y-3 rounded-lg border border-rose-900/60 bg-rose-950/40 p-4">
          <div className="text-sm font-semibold text-rose-300">
            Couldn’t load the project config
          </div>
          <p className="text-sm text-rose-200/80">
            Saving is disabled to avoid overwriting your real config with an
            empty editor. Retry the load, then edit.
          </p>
          <p className="font-mono text-xs text-rose-300/70 break-words">
            {loadError}
          </p>
          <Button variant="ghost" onClick={load}>
            Retry load
          </Button>
        </div>
      ) : (
        <textarea
          className={editorClass}
          spellCheck={false}
          autoCorrect="off"
          autoCapitalize="off"
          value={text}
          onChange={(e) => onEdit(e.target.value)}
          aria-label="Project configuration JSON editor"
        />
      )}

      {/* Server / client error panel (never a raw JSON blob) */}
      {errorPanel && (
        <div className="rounded-lg border border-rose-900/60 bg-rose-950/40 p-3">
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

      {/* Actions */}
      <div className="flex items-center gap-3">
        <Button
          variant="primary"
          onClick={save}
          disabled={!loaded || saveState === "saving" || !dirty}
        >
          {saveLabel}
        </Button>
        <Button variant="ghost" onClick={format} disabled={!loaded}>
          Format
        </Button>
        {saveState === "saved" && (
          <span className="text-sm text-emerald-400">
            Configuration saved.
          </span>
        )}
        {saveState === "error" && (
          <span className="text-sm text-rose-400">Save failed — see above.</span>
        )}
        {loaded && !dirty && saveState === "idle" && (
          <span className="text-xs text-zinc-500">No changes to save.</span>
        )}
      </div>
    </div>
  );
}
