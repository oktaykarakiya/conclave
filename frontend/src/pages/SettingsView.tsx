import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { diffConfig, isEmptyDiff, isPlainObject } from "../configDiff";
import type { Integrity, Quarantine } from "../types";
import { Badge, Button, Card, Spinner, input } from "../ui";

function asRecord(v: unknown): Record<string, unknown> {
  if (typeof v === "object" && v !== null && !Array.isArray(v)) {
    return v as Record<string, unknown>;
  }
  return {};
}

/** ui.model_choices as the textarea shows it: one model per line. */
function modelChoicesText(cfg: Record<string, unknown>): string {
  const choices = asRecord(cfg.ui).model_choices;
  return Array.isArray(choices) ? choices.map(String).join("\n") : "";
}

type ReloadStatus = Awaited<ReturnType<typeof api.reloadStatus>>;

export function SettingsView({
  projectId,
  onProjectDeleted,
}: {
  projectId: string;
  onProjectDeleted?: () => void;
}) {
  // `loaded` is the effective config as fetched; saves send only the diff against it.
  const [loaded, setLoaded] = useState<Record<string, unknown>>({});
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [rawJson, setRawJson] = useState("");
  // Raw textarea text, so typing a newline is not normalised away before the next model.
  const [modelChoices, setModelChoices] = useState("");
  const [quarantine, setQuarantine] = useState<Quarantine[]>([]);
  const [integrity, setIntegrity] = useState<Integrity | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Quarantine form state
  const [qPattern, setQPattern] = useState("");
  const [qReason, setQReason] = useState("");
  const [qUntil, setQUntil] = useState(() => {
    const d = new Date(Date.now() + 7 * 86400 * 1000);
    return d.toISOString().slice(0, 10);
  });
  const [addingQ, setAddingQ] = useState(false);

  // Raw JSON, MCP & Danger zone state
  const [showRawJson, setShowRawJson] = useState(false);
  const [confirmDetach, setConfirmDetach] = useState(false);
  const [detaching, setDetaching] = useState(false);
  const [mcpInfo, setMcpInfo] = useState<{
    mcp_url: string;
    tool_count: number;
    opencode_remote_json: Record<string, unknown>;
    opencode_stdio_json: Record<string, unknown>;
    claude_code_http_cli: string;
    claude_code_stdio_cli: string;
  } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  // Global (daemon-wide) settings: the hot-reload watcher and a manual reload
  const [reloadInfo, setReloadInfo] = useState<ReloadStatus | null>(null);
  const [globalBusy, setGlobalBusy] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setNotice(null);
    try {
      const [cfg, qList, qHealth, mcp, reload] = await Promise.all([
        api.getConfig(projectId),
        api.listQuarantine(projectId),
        api.quarantineIntegrity(projectId),
        api.mcpInfo().catch(() => null),
        api.reloadStatus().catch(() => null),
      ]);
      setLoaded(cfg);
      setConfig(cfg);
      setRawJson(JSON.stringify(cfg, null, 2));
      setModelChoices(modelChoicesText(cfg));
      setQuarantine(qList);
      setIntegrity(qHealth);
      setMcpInfo(mcp);
      setReloadInfo(reload);
    } catch (e) {
      setNotice({ kind: "err", text: String(e) });
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const execution = asRecord(config.execution);
  const experimental = asRecord(config.experimental);
  const context = asRecord(config.context);
  const notifications = asRecord(config.notifications);
  const agentDefaults = asRecord(config.agent_defaults);

  function updateSection(section: string, key: string, value: unknown) {
    setConfig((prev) => {
      const curSec = asRecord(prev[section]);
      const next = {
        ...prev,
        [section]: {
          ...curSec,
          [key]: value,
        },
      };
      setRawJson(JSON.stringify(next, null, 2));
      return next;
    });
  }

  /** Save only what `edited` changes against the loaded config, then show the fresh result. */
  async function saveChanges(edited: Record<string, unknown>, keyPrefix: string, okText: string) {
    const diff = diffConfig(loaded, edited);
    if (isEmptyDiff(diff)) {
      setNotice({ kind: "ok", text: "No changes to save." });
      return;
    }
    await api.patchConfig(
      projectId,
      diff.patch,
      `${keyPrefix}-${projectId}-${Date.now()}`,
      diff.unset,
    );
    const fresh = await api.getConfig(projectId);
    setLoaded(fresh);
    setConfig(fresh);
    setRawJson(JSON.stringify(fresh, null, 2));
    setModelChoices(modelChoicesText(fresh));
    setNotice({ kind: "ok", text: okText });
  }

  async function saveStructured() {
    setSaving(true);
    setNotice(null);
    try {
      await saveChanges(config, "cfg", "Settings saved.");
    } catch (e) {
      setNotice({ kind: "err", text: String(e) });
    } finally {
      setSaving(false);
    }
  }

  async function saveRaw() {
    setSaving(true);
    setNotice(null);
    try {
      const parsed: unknown = JSON.parse(rawJson);
      if (!isPlainObject(parsed)) {
        throw new Error("The configuration must be a JSON object.");
      }
      await saveChanges(parsed, "rawcfg", "Raw configuration applied.");
    } catch (e) {
      setNotice({ kind: "err", text: String(e) });
    } finally {
      setSaving(false);
    }
  }

  /** Toggle the daemon-wide watcher in the global layer (never written into projects). */
  async function setHotReloadWatch(enabled: boolean) {
    setGlobalBusy(true);
    setNotice(null);
    try {
      await api.patchGlobalConfig({ system: { hot_reload_watch: enabled } });
      setReloadInfo(await api.reloadStatus());
      setNotice({
        kind: "ok",
        text: `Hot-reload watcher ${enabled ? "on" : "off"} for the whole daemon.`,
      });
    } catch (e) {
      setNotice({ kind: "err", text: String(e) });
    } finally {
      setGlobalBusy(false);
    }
  }

  async function reloadNow() {
    setGlobalBusy(true);
    setNotice(null);
    try {
      const res = await api.reloadSystem(false);
      setReloadInfo(await api.reloadStatus());
      setNotice({
        kind: "ok",
        text: res.reloaded
          ? `Reloaded ${res.modules_reloaded} modules (reload #${res.reload_count}).`
          : "Reload deferred: it runs as soon as no task is in flight.",
      });
    } catch (e) {
      setNotice({ kind: "err", text: String(e) });
    } finally {
      setGlobalBusy(false);
    }
  }

  async function handleAddQuarantine(e: React.FormEvent) {
    e.preventDefault();
    if (!qPattern.trim() || !qReason.trim() || !qUntil.trim()) return;
    setAddingQ(true);
    setNotice(null);
    try {
      await api.addQuarantine(projectId, {
        pattern: qPattern.trim(),
        reason: qReason.trim(),
        until: qUntil.trim(),
      });
      setQPattern("");
      setQReason("");
      const [qList, qHealth] = await Promise.all([
        api.listQuarantine(projectId),
        api.quarantineIntegrity(projectId),
      ]);
      setQuarantine(qList);
      setIntegrity(qHealth);
    } catch (err) {
      setNotice({ kind: "err", text: String(err) });
    } finally {
      setAddingQ(false);
    }
  }

  async function handleRemoveQuarantine(qid: string) {
    try {
      await api.delQuarantine(qid);
      const [qList, qHealth] = await Promise.all([
        api.listQuarantine(projectId),
        api.quarantineIntegrity(projectId),
      ]);
      setQuarantine(qList);
      setIntegrity(qHealth);
    } catch (err) {
      setNotice({ kind: "err", text: String(err) });
    }
  }

  async function handleDetachProject() {
    setDetaching(true);
    try {
      await api.detachProject(projectId);
      onProjectDeleted?.();
    } catch (err) {
      setNotice({ kind: "err", text: String(err) });
      setDetaching(false);
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-xs text-zinc-500">
        <Spinner size={16} /> Loading project settings…
      </div>
    );
  }

  return (
    <div className="mx-auto h-full max-w-4xl space-y-6 overflow-y-auto pr-1 pb-10">
      {notice && (
        <div
          className={`flex items-center justify-between rounded-lg border px-3.5 py-2 text-xs ${
            notice.kind === "ok"
              ? "border-emerald-800/70 bg-emerald-950/50 text-emerald-300"
              : "border-rose-800/70 bg-rose-950/50 text-rose-300"
          }`}
        >
          <span>{notice.text}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="ml-4 text-zinc-400 hover:text-zinc-200"
          >
            ✕
          </button>
        </div>
      )}

      {/* Section 1: Execution & Green-Gate */}
      <Card className="space-y-4">
        <div className="flex items-center justify-between border-b border-zinc-800/80 pb-3">
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">Execution & Green-Gate</h2>
            <p className="text-xs text-zinc-500">
              Worktree provisioning, authoritative test gate commands, and retry bounds.
            </p>
          </div>
          <Button variant="primary" onClick={saveStructured} disabled={saving}>
            {saving ? "Saving…" : "Save Settings"}
          </Button>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">Target Branch</span>
            <input
              className={input}
              value={String(execution.target_branch ?? "main")}
              onChange={(e) => updateSection("execution", "target_branch", e.target.value)}
              placeholder="main"
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">Task Branch Prefix</span>
            <input
              className={input}
              value={String(execution.branch_prefix ?? "conclave/")}
              onChange={(e) => updateSection("execution", "branch_prefix", e.target.value)}
              placeholder="conclave/"
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">
              Worktree Setup Command (runs once per worktree)
            </span>
            <input
              className={input}
              value={String(execution.setup_command ?? "")}
              onChange={(e) =>
                updateSection(
                  "execution",
                  "setup_command",
                  e.target.value.trim() ? e.target.value : null,
                )
              }
              placeholder="e.g. uv sync or npm ci"
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">
              Authoritative Green-Gate Test Command
            </span>
            <input
              className={input}
              value={String(execution.baseline_test_command ?? "")}
              onChange={(e) =>
                updateSection(
                  "execution",
                  "baseline_test_command",
                  e.target.value.trim() ? e.target.value : null,
                )
              }
              placeholder="e.g. pytest -q"
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">Test Integrity Guard</span>
            <select
              className={input}
              value={String(execution.test_integrity ?? "block")}
              onChange={(e) => updateSection("execution", "test_integrity", e.target.value)}
            >
              <option value="block">block: flag to reviewers, never auto-merge</option>
              <option value="warn">warn: flag to reviewers only</option>
              <option value="off">off</option>
            </select>
            <span className="block text-[11px] text-zinc-500">
              Flags pre-existing tests a change deletes, removes or skips.
            </span>
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">Workflow evolution</span>
            <select
              className={input}
              value={String(experimental.workflow_evolution ?? "suggest")}
              onChange={(e) =>
                updateSection("experimental", "workflow_evolution", e.target.value)
              }
            >
              <option value="suggest">suggest: propose changes for you to approve</option>
              <option value="auto">auto: apply changes (each one versioned)</option>
              <option value="off">off</option>
            </select>
            <span className="block text-[11px] text-zinc-500">
              What the evolver does with the workflow after each task; review in Agent Studio
              → History.
            </span>
          </label>

          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 hover:border-zinc-700">
            <input
              type="checkbox"
              checked={Boolean(execution.allow_no_test_command ?? false)}
              onChange={(e) =>
                updateSection("execution", "allow_no_test_command", e.target.checked)
              }
              className="mt-0.5 rounded border-zinc-700 bg-zinc-900 text-indigo-600 focus:ring-indigo-500"
            />
            <span className="space-y-0.5">
              <span className="block text-xs text-zinc-300">Run without a test command</span>
              <span className="block text-[11px] text-zinc-500">
                Off: a task with no test command fails before any agent runs.
              </span>
            </span>
          </label>

          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 hover:border-zinc-700">
            <input
              type="checkbox"
              checked={Boolean(execution.auto_merge ?? false)}
              onChange={(e) => updateSection("execution", "auto_merge", e.target.checked)}
              className="mt-0.5 rounded border-zinc-700 bg-zinc-900 text-indigo-600 focus:ring-indigo-500"
            />
            <span className="space-y-0.5">
              <span className="block text-xs text-zinc-300">Auto-merge on green</span>
              <span className="block text-[11px] text-zinc-500">
                Off (default): a green task ends done on its branch; you read the diff and merge.
              </span>
            </span>
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">
              Planner threshold (request chars)
            </span>
            <input
              type="number"
              min={0}
              className={input}
              value={Number(experimental.auto_planner_char_threshold ?? 0)}
              onChange={(e) =>
                updateSection(
                  "experimental",
                  "auto_planner_char_threshold",
                  Math.max(0, Number(e.target.value) || 0),
                )
              }
            />
            <span className="block text-[11px] text-zinc-500">
              0 (default) plans every task; a task's own planner choice wins.
            </span>
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">
              Parallel Swarm Sessions (1 = single-flight, up to 16)
            </span>
            <input
              type="number"
              min={1}
              max={16}
              className={input}
              value={Number(execution.max_parallel_tasks ?? 1)}
              onChange={(e) =>
                updateSection(
                  "execution",
                  "max_parallel_tasks",
                  Math.max(1, Math.min(16, Number(e.target.value) || 1)),
                )
              }
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">
              Wall-Clock Budget (minutes, 0 = unlimited)
            </span>
            <input
              type="number"
              min={0}
              className={input}
              value={Number(execution.wall_clock_budget_minutes ?? 720)}
              onChange={(e) =>
                updateSection(
                  "execution",
                  "wall_clock_budget_minutes",
                  Math.max(0, Number(e.target.value) || 0),
                )
              }
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-zinc-400">Webhook Notification URL</span>
            <input
              className={input}
              value={String(notifications.webhook_url ?? "")}
              onChange={(e) =>
                updateSection(
                  "notifications",
                  "webhook_url",
                  e.target.value.trim() ? e.target.value : null,
                )
              }
              placeholder="https://example.com/webhook (optional)"
            />
          </label>
        </div>

        <div className="grid grid-cols-2 gap-2.5 border-t border-zinc-800/70 pt-3 sm:grid-cols-4">
          {[
            {
              section: "execution",
              key: "require_full_green",
              label: "Require green gate",
              val: Boolean(execution.require_full_green ?? true),
            },
            {
              section: "execution",
              key: "parallel_reviewers",
              label: "Parallel reviewers",
              val: Boolean(execution.parallel_reviewers ?? false),
            },
            {
              section: "experimental",
              key: "grounding_checks",
              label: "Evidence grounding",
              val: Boolean(experimental.grounding_checks ?? true),
            },
            {
              section: "experimental",
              key: "planner_enabled",
              label: "Auto planner",
              val: Boolean(experimental.planner_enabled ?? true),
            },
            {
              section: "experimental",
              key: "cross_attempt_memory",
              label: "Attempt memory",
              val: Boolean(experimental.cross_attempt_memory ?? true),
            },
            {
              section: "experimental",
              key: "post_mortem_enabled",
              label: "Failure post-mortem",
              val: Boolean(experimental.post_mortem_enabled ?? true),
            },
          ].map((item) => (
            <label
              key={`${item.section}.${item.key}`}
              className="flex cursor-pointer items-center gap-2 rounded-lg border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 text-xs text-zinc-300 hover:border-zinc-700"
            >
              <input
                type="checkbox"
                checked={item.val}
                onChange={(e) => updateSection(item.section, item.key, e.target.checked)}
                className="rounded border-zinc-700 bg-zinc-900 text-indigo-600 focus:ring-indigo-500"
              />
              <span>{item.label}</span>
            </label>
          ))}
        </div>

        <div className="space-y-3 border-t border-zinc-800/70 pt-3">
          <div>
            <h3 className="text-xs font-semibold text-zinc-300">Agents & models</h3>
            <p className="text-[11px] text-zinc-500">
              Defaults for every agent of this project; Agent Studio overrides them per agent.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <label className="block space-y-1">
              <span className="text-xs font-medium text-zinc-400">Reasoning effort</span>
              <select
                className={input}
                value={typeof agentDefaults.effort === "string" ? agentDefaults.effort : ""}
                onChange={(e) =>
                  updateSection("agent_defaults", "effort", e.target.value || null)
                }
              >
                <option value="">Auto (highest sensible per engine)</option>
                {["low", "medium", "high", "xhigh", "max"].map((level) => (
                  <option key={level} value={level}>
                    {level}
                  </option>
                ))}
              </select>
              <span className="block text-[11px] text-zinc-500">
                Auto: xhigh on Claude Code, the model's top variant on opencode; others are clamped.
              </span>
            </label>

            <label className="block space-y-1">
              <span className="text-xs font-medium text-zinc-400">
                Model picker choices (one per line)
              </span>
              <textarea
                rows={4}
                className={`${input} font-mono`}
                value={modelChoices}
                onChange={(e) => {
                  setModelChoices(e.target.value);
                  updateSection(
                    "ui",
                    "model_choices",
                    e.target.value
                      .split("\n")
                      .map((line) => line.trim())
                      .filter(Boolean),
                  );
                }}
                placeholder={"deepseek/deepseek-flash\nclaude-code/opus"}
              />
              <span className="block text-[11px] text-zinc-500">
                Empty: the built-in list for this host's engines. Models in use are always offered.
              </span>
            </label>
          </div>
        </div>

        <div className="space-y-3 border-t border-zinc-800/70 pt-3">
          <div>
            <h3 className="text-xs font-semibold text-zinc-300">Prompt context</h3>
            <p className="text-[11px] text-zinc-500">
              Requests, plans and feedback always reach agents whole. These limits apply only to
              diffs and gate/setup output; every cut is marked in the prompt and logged in Live.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 hover:border-zinc-700">
              <input
                type="checkbox"
                checked={Boolean(context.compaction_enabled ?? false)}
                onChange={(e) => updateSection("context", "compaction_enabled", e.target.checked)}
                className="mt-0.5 rounded border-zinc-700 bg-zinc-900 text-indigo-600 focus:ring-indigo-500"
              />
              <span className="space-y-0.5">
                <span className="block text-xs text-zinc-300">Compact for all agents</span>
                <span className="block text-[11px] text-zinc-500">
                  Opt-in for small-context models; an agent's compact_before_start overrides it.
                </span>
              </span>
            </label>

            <label className="block space-y-1">
              <span className="text-xs font-medium text-zinc-400">Compaction budget (chars)</span>
              <input
                type="number"
                min={1000}
                max={2000000}
                step={1000}
                className={input}
                value={Number(context.compaction_max_chars ?? 60000)}
                onChange={(e) =>
                  updateSection("context", "compaction_max_chars", Number(e.target.value) || 60000)
                }
              />
              <span className="block text-[11px] text-zinc-500">
                Per diff or output block, for agents with compaction on.
              </span>
            </label>

            <label className="block space-y-1">
              <span className="text-xs font-medium text-zinc-400">Bulk ceiling (chars)</span>
              <input
                type="number"
                min={1000}
                max={2000000}
                step={1000}
                className={input}
                value={Number(context.bulk_max_chars ?? 200000)}
                onChange={(e) =>
                  updateSection("context", "bulk_max_chars", Number(e.target.value) || 200000)
                }
              />
              <span className="block text-[11px] text-zinc-500">
                Per diff or output block when compaction is off (safety limit).
              </span>
            </label>
          </div>
        </div>
      </Card>

      {/* Section 2: Test Quarantine */}
      <Card className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">Test Quarantine</h2>
            <p className="text-xs text-zinc-500">
              Time-bounded test exclusions injected into pytest/jest during baseline and gate runs.
            </p>
          </div>
          {integrity && (
            <Badge
              text={
                integrity.healthy
                  ? `Gate Healthy · ${integrity.active} active`
                  : `${integrity.expired} expired · ${integrity.active} active`
              }
              color={
                integrity.healthy
                  ? "bg-emerald-950/80 text-emerald-300 border border-emerald-800/70"
                  : "bg-amber-950/80 text-amber-300 border border-amber-800/70"
              }
            />
          )}
        </div>

        <form
          onSubmit={handleAddQuarantine}
          className="grid grid-cols-1 gap-2.5 sm:grid-cols-12"
        >
          <input
            className={`${input} sm:col-span-4`}
            placeholder="tests/test_flaky.py::test_race"
            value={qPattern}
            onChange={(e) => setQPattern(e.target.value)}
          />
          <input
            className={`${input} sm:col-span-4`}
            placeholder="Reason for quarantine"
            value={qReason}
            onChange={(e) => setQReason(e.target.value)}
          />
          <input
            type="date"
            className={`${input} sm:col-span-2`}
            value={qUntil}
            onChange={(e) => setQUntil(e.target.value)}
          />
          <Button
            type="submit"
            variant="default"
            disabled={addingQ || !qPattern.trim() || !qReason.trim()}
            className="sm:col-span-2"
          >
            {addingQ ? "Adding…" : "+ Quarantine"}
          </Button>
        </form>

        {quarantine.length === 0 ? (
          <div className="rounded-lg border border-dashed border-zinc-800/80 py-4 text-center text-xs text-zinc-500">
            No quarantined test patterns. Full test suite is enforced.
          </div>
        ) : (
          <div className="divide-y divide-zinc-800/60 rounded-lg border border-zinc-800/80 bg-zinc-950/40">
            {quarantine.map((q) => (
              <div
                key={q.id}
                className="flex items-center justify-between gap-3 px-3 py-2 text-xs"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-zinc-200">{q.pattern}</div>
                  <div className="truncate text-[11px] text-zinc-500">
                    {q.reason} · until {q.until}
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => handleRemoveQuarantine(q.id)}
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Section 3: MCP Connect (OpenCode & Claude Code) */}
      {mcpInfo && (
        <Card className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
            <div>
              <h2 className="text-sm font-semibold text-zinc-100">
                MCP Server · Connect OpenCode or Claude Code
              </h2>
              <p className="text-xs text-zinc-500">
                Control projects, tasks, live steer, workflow diagrams, and personas directly from your CLI LLM.
              </p>
            </div>
            <Badge
              text={`${mcpInfo.tool_count} MCP Tools · ${mcpInfo.mcp_url}`}
              color="bg-indigo-950/80 text-indigo-300 border border-indigo-800/70"
            />
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5 rounded-lg border border-zinc-800/80 bg-zinc-950/60 p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-zinc-300">
                  OpenCode (`opencode.json` Remote HTTP)
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    navigator.clipboard?.writeText(
                      JSON.stringify(mcpInfo.opencode_remote_json, null, 2),
                    );
                    setCopiedKey("opencode_http");
                    setTimeout(() => setCopiedKey(null), 1800);
                  }}
                >
                  {copiedKey === "opencode_http" ? "Copied ✓" : "Copy"}
                </Button>
              </div>
              <pre className="overflow-x-auto font-mono text-[11px] text-zinc-400">
                {JSON.stringify(mcpInfo.opencode_remote_json, null, 2)}
              </pre>
            </div>

            <div className="space-y-2 rounded-lg border border-zinc-800/80 bg-zinc-950/60 p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-zinc-300">
                  Claude Code CLI (HTTP & Stdio)
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    navigator.clipboard?.writeText(mcpInfo.claude_code_http_cli);
                    setCopiedKey("claude_http");
                    setTimeout(() => setCopiedKey(null), 1800);
                  }}
                >
                  {copiedKey === "claude_http" ? "Copied ✓" : "Copy HTTP CLI"}
                </Button>
              </div>
              <pre className="overflow-x-auto rounded bg-zinc-900/90 p-2 font-mono text-[11px] text-indigo-300">
                {mcpInfo.claude_code_http_cli}
              </pre>
              <div className="flex items-center justify-between pt-1">
                <span className="text-[11px] text-zinc-500">Or via stdio bridge:</span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    navigator.clipboard?.writeText(mcpInfo.claude_code_stdio_cli);
                    setCopiedKey("claude_stdio");
                    setTimeout(() => setCopiedKey(null), 1800);
                  }}
                >
                  {copiedKey === "claude_stdio" ? "Copied ✓" : "Copy Stdio CLI"}
                </Button>
              </div>
              <pre className="overflow-x-auto rounded bg-zinc-900/90 p-2 font-mono text-[11px] text-zinc-400">
                {mcpInfo.claude_code_stdio_cli}
              </pre>
            </div>
          </div>
        </Card>
      )}

      {/* Section 4: Global (daemon-wide) settings */}
      <Card className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-3">
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">Global settings (all projects)</h2>
            <p className="text-xs text-zinc-500">
              Daemon-wide: stored in the global settings and applied to every project at once.
            </p>
          </div>
          <Button variant="default" size="sm" onClick={reloadNow} disabled={globalBusy}>
            {globalBusy ? "Working…" : "Reload now"}
          </Button>
        </div>

        <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 hover:border-zinc-700">
          <input
            type="checkbox"
            checked={Boolean(reloadInfo?.watcher_enabled)}
            disabled={globalBusy || reloadInfo === null}
            onChange={(e) => void setHotReloadWatch(e.target.checked)}
            className="mt-0.5 rounded border-zinc-700 bg-zinc-900 text-indigo-600 focus:ring-indigo-500"
          />
          <span className="space-y-0.5">
            <span className="block text-xs text-zinc-300">Hot-reload watcher</span>
            <span className="block text-[11px] text-zinc-500">
              Off (default): Conclave reloads its own code only when you press Reload now.
            </span>
          </span>
        </label>

        {reloadInfo && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-500">
            <span>Watcher: {reloadInfo.watcher_enabled ? "on" : "off"}</span>
            <span>Reload pending: {reloadInfo.reload_pending ? "yes" : "no"}</span>
            <span>Changed files since last reload: {reloadInfo.changed_files.length}</span>
            <span>Reloads: {reloadInfo.reload_count}</span>
          </div>
        )}
      </Card>

      {/* Section 5: Advanced Raw JSON & Danger Zone */}
      <Card className="space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">Advanced Configuration JSON</h2>
            <p className="text-xs text-zinc-500">
              Inspect or edit the effective configuration. Only what you change is stored for
              this project; delete a key to inherit it from the global settings again.
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => setShowRawJson((v) => !v)}>
            {showRawJson ? "Hide JSON" : "Edit Raw JSON"}
          </Button>
        </div>

        {showRawJson && (
          <div className="space-y-2 pt-2">
            <textarea
              rows={14}
              value={rawJson}
              onChange={(e) => setRawJson(e.target.value)}
              className="w-full rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-200 outline-none focus:border-indigo-500"
            />
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  try {
                    setRawJson(JSON.stringify(JSON.parse(rawJson), null, 2));
                  } catch {
                    /* ignore */
                  }
                }}
              >
                Format JSON
              </Button>
              <Button variant="primary" size="sm" onClick={saveRaw} disabled={saving}>
                Apply Raw JSON
              </Button>
            </div>
          </div>
        )}
      </Card>

      <Card className="flex items-center justify-between border-rose-900/40 bg-rose-950/10">
        <div>
          <h2 className="text-sm font-semibold text-rose-200">Detach Repository</h2>
          <p className="text-xs text-zinc-500">
            Stops the project worker, cleans active worktrees, and removes this project from Conclave.
          </p>
        </div>
        {!confirmDetach ? (
          <Button variant="danger" size="sm" onClick={() => setConfirmDetach(true)}>
            Detach Project
          </Button>
        ) : (
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setConfirmDetach(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={handleDetachProject}
              disabled={detaching}
            >
              {detaching ? "Detaching…" : "Confirm Detach"}
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
