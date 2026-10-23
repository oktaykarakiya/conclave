import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import type {
  EventRow,
  InteractionLog,
  InteractionMessage,
  ObservabilityStats,
  Project,
  SqlQueryResult,
} from "../types";
import { Badge, Button, Card, Spinner, input } from "../ui";

interface SqlPreset {
  label: string;
  sql: string;
}

const SQL_PRESETS: SqlPreset[] = [
  {
    label: "Recent Sessions",
    sql: "SELECT session_id, title, agent, model, ROUND(cost, 5) AS cost, tokens_input, tokens_output, duration, created_at_str FROM interaction_logs ORDER BY time_updated DESC LIMIT 50",
  },
  {
    label: "Cost by Agent",
    sql: "SELECT agent, COUNT(*) AS sessions, ROUND(SUM(cost), 5) AS total_cost, SUM(tokens_input) AS in_tok, SUM(tokens_output) AS out_tok, SUM(tool_calls_count) AS tools FROM interaction_logs GROUP BY agent ORDER BY total_cost DESC",
  },
  {
    label: "Cost by Model",
    sql: "SELECT model, COUNT(*) AS sessions, ROUND(SUM(cost), 5) AS total_cost, SUM(tokens_input) AS in_tok, SUM(tokens_output) AS out_tok FROM interaction_logs GROUP BY model ORDER BY total_cost DESC",
  },
  {
    label: "MCP Tool Calls",
    sql: "SELECT session_id, title, user_prompt, created_at_str FROM interaction_logs WHERE source = 'mcp' ORDER BY time_updated DESC LIMIT 50",
  },
  {
    label: "HITL & Operator Actions",
    sql: "SELECT session_id, title, agent, node_id, user_prompt, created_at_str FROM interaction_logs WHERE source = 'operator' ORDER BY time_updated DESC LIMIT 50",
  },
];

const PROMPT_SECTION_HEADERS = [
  "SYSTEM CONTEXT",
  "SKILLS & CAPABILITIES",
  "REPO KNOWLEDGE",
  "PROJECT RULES",
  "TASK CONTEXT",
  "TASK",
] as const;

interface PromptSection {
  header: string;
  body: string;
  defaultOpen: boolean;
}

function parsePromptSections(text: string): PromptSection[] | null {
  const pattern = new RegExp(
    `(?:^|\\n\\n)(${PROMPT_SECTION_HEADERS.map((h) => h.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")}):\\n`,
    "g",
  );
  const matches: { header: string; startIdx: number; contentStartIdx: number }[] = [];
  let match = pattern.exec(text);
  while (match !== null) {
    const leadingOffset = match[0].startsWith("\n\n") ? 2 : 0;
    matches.push({
      header: match[1],
      startIdx: match.index + leadingOffset,
      contentStartIdx: match.index + match[0].length,
    });
    match = pattern.exec(text);
  }

  if (matches.length === 0) {
    return null;
  }

  const sections: PromptSection[] = [];
  if (matches[0].startIdx > 0) {
    const preamble = text.slice(0, matches[0].startIdx).trim();
    if (preamble) {
      sections.push({
        header: "PREAMBLE",
        body: preamble,
        defaultOpen: preamble.length <= 600,
      });
    }
  }

  for (let i = 0; i < matches.length; i++) {
    const current = matches[i];
    const nextStart = i + 1 < matches.length ? matches[i + 1].startIdx : text.length;
    const body = text.slice(current.contentStartIdx, nextStart).trim();
    sections.push({
      header: current.header,
      body,
      defaultOpen: current.header === "TASK" || current.header === "SKILLS & CAPABILITIES",
    });
  }

  return sections;
}

function formatTimestamp(row: InteractionLog): string {
  if (row.updated_at_str && row.updated_at_str.trim()) {
    return row.updated_at_str;
  }
  if (row.created_at_str && row.created_at_str.trim()) {
    return row.created_at_str;
  }
  if (row.time_updated > 0) {
    const d = new Date(row.time_updated);
    return d.toISOString().replace("T", " ").slice(0, 19);
  }
  return "—";
}

function formatByteSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB (${bytes.toLocaleString()} B)`;
  }
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(1)} KB (${bytes.toLocaleString()} B)`;
  }
  return `${bytes.toLocaleString()} B`;
}

function sessionToMarkdown(session: InteractionLog): string {
  const lines: string[] = [
    `# ${session.title || "Untitled Session"}`,
    "",
    `- **Session ID**: \`${session.session_id}\``,
    `- **Agent**: \`${session.agent || "unknown"}\` (${session.source || "conclave"})`,
    `- **Model**: \`${session.model || "unknown"}\``,
    `- **Directory**: \`${session.directory || "—"}\``,
    `- **Created**: ${session.created_at_str || "—"}`,
    `- **Duration**: ${session.duration.toFixed(1)}s`,
    `- **Cost**: $${session.cost.toFixed(5)}`,
    `- **Tokens**: ${session.tokens_input.toLocaleString()} in / ${session.tokens_output.toLocaleString()} out / ${session.tokens_reasoning.toLocaleString()} reasoning / ${session.tokens_cache_read.toLocaleString()} cache`,
  ];
  if (session.task_id) lines.push(`- **Task ID**: \`${session.task_id}\``);
  if (session.node_id) lines.push(`- **Node ID**: \`${session.node_id}\``);

  const tools = Object.entries(session.tool_breakdown ?? {});
  if (tools.length > 0) {
    lines.push(
      `- **Tools (${session.tool_calls_count})**: ${tools.map(([k, v]) => `${k} (${v})`).join(", ")}`,
    );
  }

  lines.push("", "---", "");
  for (const m of session.messages ?? []) {
    const roleHeader = m.role.toUpperCase();
    const meta = [m.time, m.model, m.tokens].filter(Boolean).join(" · ");
    lines.push(`## ${roleHeader}${meta ? ` (${meta})` : ""}`, "", m.text || "", "");
  }
  return lines.join("\n");
}

function TranscriptMessageCard({
  message,
  index,
}: {
  message: InteractionMessage;
  index: number;
}) {
  const role = (message.role || "unknown").toLowerCase();
  const isUser = role === "user";
  const text = message.text || "";
  const sections = useMemo(() => parsePromptSections(text), [text]);
  const isLongPlain = !sections && text.length > 1200;
  const [expandedPlain, setExpandedPlain] = useState(!isLongPlain);

  return (
    <div
      className={`rounded-xl border p-3.5 ${
        isUser
          ? "border-zinc-800/90 bg-zinc-900/70"
          : "border-indigo-900/50 bg-indigo-950/15"
      }`}
    >
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/70 pb-2">
        <div className="flex items-center gap-2">
          <Badge
            text={isUser ? "USER / SYSTEM PROMPT" : "ASSISTANT RESPONSE"}
            color={
              isUser
                ? "bg-sky-950/80 text-sky-300 border border-sky-800/60"
                : "bg-indigo-950/80 text-indigo-300 border border-indigo-800/60"
            }
          />
          <span className="font-mono text-[11px] text-zinc-500">#{index + 1}</span>
          {message.model && (
            <span className="rounded bg-zinc-900 px-1.5 py-0.5 font-mono text-[11px] text-zinc-400 ring-1 ring-zinc-800">
              {message.model}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-[11px] text-zinc-500">
          {message.tokens && <span className="font-mono">{message.tokens}</span>}
          {message.time && <span className="font-mono">{message.time}</span>}
        </div>
      </div>

      {message.tools && message.tools.length > 0 && (
        <div className="mb-2.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Tools:
          </span>
          {message.tools.map((t, idx) => (
            <span
              key={`${t.name}-${idx}`}
              className="inline-flex items-center gap-1 rounded border border-emerald-800/50 bg-emerald-950/40 px-2 py-0.5 font-mono text-[11px] text-emerald-300"
            >
              {t.name}
              {t.count !== undefined && t.count > 1 ? ` ×${t.count}` : ""}
            </span>
          ))}
        </div>
      )}

      {sections ? (
        <div className="space-y-2">
          {sections.map((sec, idx) => (
            <details
              key={`${sec.header}-${idx}`}
              open={sec.defaultOpen}
              className="group rounded-lg border border-zinc-800/80 bg-zinc-950/70"
            >
              <summary className="flex cursor-pointer items-center justify-between gap-2 px-3 py-1.5 text-xs font-medium text-zinc-300 select-none hover:bg-zinc-900/80">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] font-semibold tracking-wide text-indigo-300">
                    {sec.header}
                  </span>
                  <span className="font-mono text-[10px] text-zinc-500">
                    {sec.body.length.toLocaleString()} chars
                  </span>
                </div>
                <span className="text-[10px] text-zinc-500 group-open:hidden">
                  Click to expand
                </span>
                <span className="hidden text-[10px] text-zinc-500 group-open:inline">
                  Collapse
                </span>
              </summary>
              <pre className="max-h-96 overflow-auto border-t border-zinc-800/70 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-words text-zinc-200">
                {sec.body || "(empty)"}
              </pre>
            </details>
          ))}
        </div>
      ) : (
        <div>
          {isLongPlain && (
            <div className="mb-1.5 flex items-center justify-between">
              <span className="font-mono text-[10px] text-zinc-500">
                {text.length.toLocaleString()} chars
              </span>
              <button
                type="button"
                onClick={() => setExpandedPlain((prev) => !prev)}
                className="text-[11px] font-medium text-indigo-400 hover:text-indigo-300"
              >
                {expandedPlain ? "Collapse long message" : "Expand full message"}
              </button>
            </div>
          )}
          <pre
            className={`overflow-auto rounded-lg border border-zinc-800/60 bg-zinc-950/60 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-words text-zinc-200 ${
              expandedPlain ? "max-h-[32rem]" : "max-h-40"
            }`}
          >
            {text || "(empty message)"}
          </pre>
        </div>
      )}
    </div>
  );
}

export function HistoryView({
  project,
  lastEvent,
}: {
  project: Project | null;
  lastEvent?: EventRow | null;
}) {
  // Scope: All Workspace History (false) vs Current Repo Only (true)
  const [scopeProject, setScopeProject] = useState<boolean>(false);

  // Filters
  const [search, setSearch] = useState("");
  const [filterAgent, setFilterAgent] = useState("");
  const [filterModel, setFilterModel] = useState("");
  const [filterSource, setFilterSource] = useState<"all" | "conclave" | "mcp">("all");

  // Data state
  const [stats, setStats] = useState<ObservabilityStats | null>(null);
  const [sessions, setSessions] = useState<InteractionLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Parquet sync state
  const [syncingParquet, setSyncingParquet] = useState(false);
  const [parquetBadge, setParquetBadge] = useState<{
    ok: boolean;
    path: string;
    rows: number;
    size_bytes: number;
  } | null>(null);

  // SQL Console state
  const [showSqlConsole, setShowSqlConsole] = useState(false);
  const [sql, setSql] = useState(SQL_PRESETS[0].sql);
  const [sqlRunning, setSqlRunning] = useState(false);
  const [sqlError, setSqlError] = useState<string | null>(null);
  const [sqlResult, setSqlResult] = useState<SqlQueryResult | null>(null);

  // Full Transcript Inspector Drawer state
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedSession, setSelectedSession] = useState<InteractionLog | null>(null);
  const [loadingSession, setLoadingSession] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [copiedMode, setCopiedMode] = useState<"json" | "md" | null>(null);

  const hasLoadedOnce = useRef(false);

  const effectiveProjectId = scopeProject ? project?.id : undefined;

  const loadData = useCallback(
    async (silent = false) => {
      if (!silent && !hasLoadedOnce.current) {
        setLoading(true);
      }
      setError(null);
      try {
        const [freshStats, freshSessions] = await Promise.all([
          api.observabilityStats(effectiveProjectId),
          api.observabilitySessions({
            project_id: effectiveProjectId,
            agent: filterAgent || undefined,
            model: filterModel || undefined,
            search: search || undefined,
            limit: 100,
          }),
        ]);
        setStats(freshStats);
        setSessions(freshSessions);
        hasLoadedOnce.current = true;
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [effectiveProjectId, filterAgent, filterModel, search],
  );

  useEffect(() => {
    void loadData(false);
  }, [loadData]);

  // Real-time auto-sync when interaction.logged, mcp.tool_called, or task.done arrives
  useEffect(() => {
    if (!lastEvent) return;
    if (
      lastEvent.type === "interaction.logged" ||
      lastEvent.type === "mcp.tool_called" ||
      lastEvent.type === "task.done"
    ) {
      void loadData(true);
      const eventSessionId =
        typeof lastEvent.payload?.session_id === "string"
          ? lastEvent.payload.session_id
          : null;
      if (selectedSessionId && eventSessionId === selectedSessionId) {
        api
          .observabilitySession(selectedSessionId)
          .then((full) => setSelectedSession(full))
          .catch(() => {
            /* preserve drawer state on transient error */
          });
      }
    }
  }, [lastEvent, loadData, selectedSessionId]);

  const filteredSessions = useMemo(() => {
    if (filterSource === "all") return sessions;
    return sessions.filter(
      (row) => (row.source || "conclave").toLowerCase() === filterSource,
    );
  }, [sessions, filterSource]);

  const openTranscriptDrawer = useCallback(async (sessionId: string) => {
    setSelectedSessionId(sessionId);
    setLoadingSession(true);
    setSessionError(null);
    try {
      const full = await api.observabilitySession(sessionId);
      setSelectedSession(full);
    } catch (e) {
      setSessionError(String(e));
    } finally {
      setLoadingSession(false);
    }
  }, []);

  const closeTranscriptDrawer = useCallback(() => {
    setSelectedSessionId(null);
    setSelectedSession(null);
    setSessionError(null);
  }, []);

  async function handleExportParquet() {
    setSyncingParquet(true);
    setError(null);
    try {
      const res = await api.exportParquet();
      setParquetBadge(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setSyncingParquet(false);
    }
  }

  const executeSqlQuery = useCallback(async (queryText: string) => {
    const trimmed = queryText.trim();
    if (!trimmed) return;
    setSqlRunning(true);
    setSqlError(null);
    try {
      const res = await api.observabilityQuery(trimmed);
      setSqlResult(res);
    } catch (e) {
      setSqlError(String(e));
    } finally {
      setSqlRunning(false);
    }
  }, []);

  function handlePresetClick(preset: SqlPreset) {
    setSql(preset.sql);
    void executeSqlQuery(preset.sql);
  }

  async function copyTranscript(mode: "json" | "md") {
    if (!selectedSession) return;
    const content =
      mode === "json"
        ? JSON.stringify(selectedSession, null, 2)
        : sessionToMarkdown(selectedSession);
    try {
      await navigator.clipboard.writeText(content);
      setCopiedMode(mode);
      setTimeout(() => {
        setCopiedMode((cur) => (cur === mode ? null : cur));
      }, 2000);
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col gap-3 overflow-hidden">
      {/* Top Bar: Scope Toggle & Action Controls */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex items-center rounded-lg border border-zinc-800/90 bg-zinc-900/70 p-0.5">
            <button
              type="button"
              onClick={() => setScopeProject(false)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                !scopeProject
                  ? "bg-indigo-600 text-white"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              All Workspace History
            </button>
            {project && (
              <button
                type="button"
                onClick={() => setScopeProject(true)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  scopeProject
                    ? "bg-indigo-600 text-white"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                Current Repo Only ({project.name})
              </button>
            )}
          </div>

          {parquetBadge && (
            <div className="inline-flex items-center gap-2 rounded-lg border border-emerald-800/70 bg-emerald-950/50 px-2.5 py-1 text-[11px] text-emerald-300">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
              <span className="font-mono">{parquetBadge.path}</span>
              <span>·</span>
              <span className="font-semibold">
                {parquetBadge.rows.toLocaleString()} rows
              </span>
              <span>·</span>
              <span className="font-mono">
                ZSTD {formatByteSize(parquetBadge.size_bytes)}
              </span>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant={showSqlConsole ? "primary" : "default"}
            size="sm"
            onClick={() => setShowSqlConsole((prev) => !prev)}
          >
            {showSqlConsole ? "Hide SQL Console" : "SQL Console"}
          </Button>
          <Button
            variant="default"
            size="sm"
            disabled={syncingParquet}
            onClick={handleExportParquet}
          >
            {syncingParquet ? (
              <>
                <Spinner size={12} />
                Syncing Parquet…
              </>
            ) : (
              "Sync INTERACTION_LOGS.parquet"
            )}
          </Button>
        </div>
      </div>

      {/* 6 Crisp KPI Cards */}
      <div className="grid shrink-0 grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6">
        <Card className="p-3">
          <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Sessions
          </div>
          <div className="mt-1 font-mono text-lg font-semibold text-zinc-100">
            {(stats?.total_sessions ?? 0).toLocaleString()}
          </div>
          <div className="mt-0.5 text-[11px] text-zinc-500">
            {(stats?.total_messages ?? 0).toLocaleString()} messages
          </div>
        </Card>

        <Card className="p-3">
          <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Total Cost
          </div>
          <div className="mt-1 font-mono text-lg font-semibold text-emerald-400">
            ${(stats?.total_cost ?? 0).toFixed(4)}
          </div>
          <div className="mt-0.5 text-[11px] text-zinc-500">
            Cumulative USD spend
          </div>
        </Card>

        <Card className="p-3">
          <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Input / Output Tokens
          </div>
          <div className="mt-1 truncate font-mono text-sm font-semibold text-sky-300">
            {(stats?.total_input_tokens ?? 0).toLocaleString()} /{" "}
            {(stats?.total_output_tokens ?? 0).toLocaleString()}
          </div>
          <div className="mt-0.5 text-[11px] text-zinc-500">
            Prompt in / completion out
          </div>
        </Card>

        <Card className="p-3">
          <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Reasoning / Cache Read
          </div>
          <div className="mt-1 truncate font-mono text-sm font-semibold text-violet-300">
            {(stats?.total_reasoning_tokens ?? 0).toLocaleString()} /{" "}
            {(stats?.total_cache_read_tokens ?? 0).toLocaleString()}
          </div>
          <div className="mt-0.5 text-[11px] text-zinc-500">
            Thinking / cached tokens
          </div>
        </Card>

        <Card className="p-3">
          <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Tool Calls
          </div>
          <div className="mt-1 font-mono text-lg font-semibold text-amber-300">
            {(stats?.total_tool_calls ?? 0).toLocaleString()}
          </div>
          <div className="mt-0.5 text-[11px] text-zinc-500">
            Total CLI & MCP invocations
          </div>
        </Card>

        <Card className="p-3">
          <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Avg Duration
          </div>
          <div className="mt-1 font-mono text-lg font-semibold text-zinc-100">
            {(stats?.avg_duration ?? 0).toFixed(1)}s
          </div>
          <div className="mt-0.5 text-[11px] text-zinc-500">
            Mean turn latency
          </div>
        </Card>
      </div>

      {error && (
        <div className="shrink-0 rounded-lg border border-rose-800/70 bg-rose-950/40 px-3 py-2 text-xs text-rose-300">
          {error}
        </div>
      )}

      {/* Interactive ANSI SQL Query Console (Collapsible) */}
      {showSqlConsole && (
        <Card className="flex max-h-80 shrink-0 flex-col gap-2.5 overflow-hidden p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-xs font-semibold text-zinc-200">
                ANSI SQL Console
              </span>
              {SQL_PRESETS.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  onClick={() => handlePresetClick(preset)}
                  className="rounded-md border border-zinc-700/80 bg-zinc-800/80 px-2 py-0.5 text-[11px] font-medium text-zinc-300 transition hover:border-indigo-500/60 hover:bg-indigo-950/50 hover:text-indigo-200"
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              {sqlResult && (
                <span className="font-mono text-[11px] text-zinc-400">
                  {sqlResult.row_count} rows
                </span>
              )}
              <Button
                variant="primary"
                size="sm"
                disabled={sqlRunning || !sql.trim()}
                onClick={() => void executeSqlQuery(sql)}
              >
                {sqlRunning ? (
                  <>
                    <Spinner size={12} />
                    Running…
                  </>
                ) : (
                  "Run Query (SELECT)"
                )}
              </Button>
            </div>
          </div>

          <textarea
            value={sql}
            onChange={(e) => setSql(e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                e.preventDefault();
                void executeSqlQuery(sql);
              }
            }}
            rows={2}
            placeholder="SELECT * FROM interaction_logs ORDER BY time_updated DESC LIMIT 50"
            className={`${input} shrink-0 font-mono text-xs`}
          />

          {sqlError && (
            <div className="rounded-lg border border-rose-800/70 bg-rose-950/40 px-2.5 py-1.5 font-mono text-xs text-rose-300">
              {sqlError}
            </div>
          )}

          {sqlResult && (
            <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-zinc-800/80 bg-zinc-950/80">
              {sqlResult.columns.length === 0 || sqlResult.rows.length === 0 ? (
                <div className="p-4 text-center font-mono text-xs text-zinc-500">
                  Query returned 0 rows.
                </div>
              ) : (
                <table className="w-full border-collapse text-left font-mono text-xs">
                  <thead className="sticky top-0 bg-zinc-900 text-[10px] font-semibold tracking-wider text-zinc-400 uppercase">
                    <tr>
                      {sqlResult.columns.map((col) => (
                        <th
                          key={col}
                          className="border-b border-zinc-800 px-2.5 py-1.5 whitespace-nowrap"
                        >
                          {col}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800/60">
                    {sqlResult.rows.map((r, rIdx) => {
                      const rowSessionId =
                        typeof r.session_id === "string" ? r.session_id : null;
                      return (
                        <tr
                          key={rIdx}
                          onClick={() => {
                            if (rowSessionId) {
                              void openTranscriptDrawer(rowSessionId);
                            }
                          }}
                          className={
                            rowSessionId
                              ? "cursor-pointer hover:bg-zinc-900/90"
                              : "hover:bg-zinc-900/50"
                          }
                        >
                          {sqlResult.columns.map((col) => {
                            const val = r[col];
                            return (
                              <td
                                key={col}
                                className="max-w-xs truncate px-2.5 py-1.5 whitespace-nowrap text-zinc-300"
                              >
                                {val === null || val === undefined
                                  ? "null"
                                  : typeof val === "object"
                                    ? JSON.stringify(val)
                                    : String(val)}
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Filter Bar */}
      <div className="grid shrink-0 grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5">
        <div className="lg:col-span-2">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search title, prompt, session_id, directory…"
            className={input}
          />
        </div>

        <select
          value={filterAgent}
          onChange={(e) => setFilterAgent(e.target.value)}
          aria-label="Filter by agent"
          className={input}
        >
          <option value="">All Agents ({stats?.agents.length ?? 0})</option>
          {(stats?.agents ?? []).map((a) => (
            <option key={a.agent} value={a.agent}>
              {a.agent} ({a.count})
            </option>
          ))}
        </select>

        <select
          value={filterModel}
          onChange={(e) => setFilterModel(e.target.value)}
          aria-label="Filter by model"
          className={input}
        >
          <option value="">All Models ({stats?.models.length ?? 0})</option>
          {(stats?.models ?? []).map((m) => (
            <option key={m.model} value={m.model}>
              {m.model} ({m.count})
            </option>
          ))}
        </select>

        <select
          value={filterSource}
          onChange={(e) =>
            setFilterSource(e.target.value as "all" | "conclave" | "mcp")
          }
          aria-label="Filter by source"
          className={input}
        >
          <option value="all">Source: All</option>
          <option value="conclave">Source: conclave</option>
          <option value="mcp">Source: mcp</option>
        </select>
      </div>

      {/* Conversation Ledger Table */}
      <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-zinc-800/80 bg-zinc-900/40">
        {loading && !hasLoadedOnce.current ? (
          <div className="flex h-48 items-center justify-center gap-2 text-xs text-zinc-500">
            <Spinner size={16} />
            Loading interaction ledger…
          </div>
        ) : filteredSessions.length === 0 ? (
          <div className="flex h-48 flex-col items-center justify-center px-4 text-center">
            <div className="text-xs font-medium text-zinc-400">
              No interaction sessions match the current filters.
            </div>
            <div className="mt-1 text-[11px] text-zinc-500">
              Run a task, planning turn, or MCP tool call to populate real-time session telemetry.
            </div>
          </div>
        ) : (
          <table className="w-full border-collapse text-left text-xs">
            <thead className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/95 text-[10px] font-semibold tracking-wider text-zinc-400 uppercase backdrop-blur-xs">
              <tr>
                <th className="px-3 py-2.5 whitespace-nowrap">Timestamp</th>
                <th className="px-3 py-2.5">Title & Session</th>
                <th className="px-3 py-2.5 whitespace-nowrap">Source & Agent</th>
                <th className="px-3 py-2.5 whitespace-nowrap">Model</th>
                <th className="px-3 py-2.5 whitespace-nowrap">
                  Tokens (In / Out / Rsn)
                </th>
                <th className="px-3 py-2.5 text-right whitespace-nowrap">Tools</th>
                <th className="px-3 py-2.5 text-right whitespace-nowrap">Duration</th>
                <th className="px-3 py-2.5 text-right whitespace-nowrap">Cost</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/60">
              {filteredSessions.map((row) => {
                const isSelected = selectedSessionId === row.session_id;
                const isMcp = (row.source || "").toLowerCase() === "mcp";
                return (
                  <tr
                    key={row.session_id}
                    onClick={() => void openTranscriptDrawer(row.session_id)}
                    className={`cursor-pointer transition-colors ${
                      isSelected
                        ? "bg-indigo-950/40"
                        : "hover:bg-zinc-800/50"
                    }`}
                  >
                    <td className="px-3 py-2.5 font-mono text-[11px] whitespace-nowrap text-zinc-400">
                      {formatTimestamp(row)}
                    </td>
                    <td className="max-w-xs px-3 py-2.5">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-medium text-zinc-100">
                          {row.title || "Untitled Session"}
                        </span>
                        <span className="shrink-0 rounded bg-zinc-800/90 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400 ring-1 ring-zinc-700/70">
                          {row.session_id.slice(0, 12)}
                        </span>
                      </div>
                      {row.user_prompt && (
                        <div className="mt-0.5 truncate text-[11px] text-zinc-500">
                          {row.user_prompt}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 whitespace-nowrap">
                      <div className="flex items-center gap-1.5">
                        <Badge
                          text={row.source || "conclave"}
                          color={
                            isMcp
                              ? "bg-teal-950/80 text-teal-300 border border-teal-800/60"
                              : "bg-indigo-950/80 text-indigo-300 border border-indigo-800/60"
                          }
                        />
                        <Badge
                          text={row.agent || "agent"}
                          color="bg-zinc-800 text-zinc-200 border border-zinc-700"
                        />
                      </div>
                    </td>
                    <td className="px-3 py-2.5 whitespace-nowrap">
                      <Badge
                        text={row.model || "default"}
                        color="bg-zinc-900 text-zinc-300 border border-zinc-800 font-mono"
                      />
                    </td>
                    <td className="px-3 py-2.5 font-mono text-[11px] whitespace-nowrap text-zinc-300">
                      <span className="text-sky-300">
                        {row.tokens_input.toLocaleString()}
                      </span>{" "}
                      in /{" "}
                      <span className="text-emerald-300">
                        {row.tokens_output.toLocaleString()}
                      </span>{" "}
                      out /{" "}
                      <span className="text-violet-300">
                        {row.tokens_reasoning.toLocaleString()}
                      </span>{" "}
                      rsn
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-[11px] whitespace-nowrap text-amber-300">
                      {row.tool_calls_count}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-[11px] whitespace-nowrap text-zinc-300">
                      {row.duration.toFixed(1)}s
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-[11px] font-semibold whitespace-nowrap text-emerald-400">
                      ${row.cost.toFixed(5)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Full Transcript Inspector Drawer on the Right */}
      {selectedSessionId && (
        <div
          className="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-xs"
          onClick={closeTranscriptDrawer}
        >
          <aside
            className="flex h-full w-full max-w-3xl flex-col border-l border-zinc-800 bg-zinc-950 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Drawer Header */}
            <div className="flex shrink-0 items-start justify-between gap-3 border-b border-zinc-800 px-5 py-3.5">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="truncate text-sm font-semibold text-zinc-100">
                    {selectedSession?.title || "Session Transcript Inspector"}
                  </h3>
                  <span className="rounded bg-zinc-900 px-2 py-0.5 font-mono text-[11px] text-indigo-300 ring-1 ring-zinc-800">
                    {selectedSessionId}
                  </span>
                </div>
                {selectedSession && (
                  <div className="mt-1 truncate font-mono text-[11px] text-zinc-500">
                    {selectedSession.directory || "—"}
                  </div>
                )}
              </div>

              <div className="flex shrink-0 items-center gap-2">
                {selectedSession && (
                  <>
                    <Button
                      variant="default"
                      size="sm"
                      onClick={() => void copyTranscript("json")}
                    >
                      {copiedMode === "json" ? "Copied JSON" : "Copy Full JSON"}
                    </Button>
                    <Button
                      variant="default"
                      size="sm"
                      onClick={() => void copyTranscript("md")}
                    >
                      {copiedMode === "md" ? "Copied Markdown" : "Copy Markdown"}
                    </Button>
                  </>
                )}
                <Button variant="ghost" size="sm" onClick={closeTranscriptDrawer}>
                  ✕
                </Button>
              </div>
            </div>

            {/* Drawer Body */}
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
              {loadingSession && !selectedSession ? (
                <div className="flex h-48 items-center justify-center gap-2 text-xs text-zinc-500">
                  <Spinner size={16} />
                  Loading full session transcript…
                </div>
              ) : sessionError ? (
                <div className="rounded-lg border border-rose-800/70 bg-rose-950/40 p-3 text-xs text-rose-300">
                  {sessionError}
                </div>
              ) : selectedSession ? (
                <>
                  {/* Session Metadata Grid */}
                  <div className="grid grid-cols-2 gap-2.5 rounded-xl border border-zinc-800/80 bg-zinc-900/50 p-3.5 text-xs sm:grid-cols-4">
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Agent & Source
                      </div>
                      <div className="mt-0.5 font-mono text-zinc-200">
                        {selectedSession.agent || "—"} ({selectedSession.source || "conclave"})
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Model
                      </div>
                      <div className="mt-0.5 truncate font-mono text-zinc-200">
                        {selectedSession.model || "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Created At
                      </div>
                      <div className="mt-0.5 font-mono text-zinc-200">
                        {selectedSession.created_at_str || "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Duration & Cost
                      </div>
                      <div className="mt-0.5 font-mono text-emerald-400">
                        {selectedSession.duration.toFixed(1)}s · $
                        {selectedSession.cost.toFixed(5)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Session ID
                      </div>
                      <div className="mt-0.5 truncate font-mono text-zinc-300">
                        {selectedSession.session_id}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Directory
                      </div>
                      <div className="mt-0.5 truncate font-mono text-zinc-300">
                        {selectedSession.directory || "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Task ID
                      </div>
                      <div className="mt-0.5 truncate font-mono text-zinc-300">
                        {selectedSession.task_id || "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold text-zinc-500 uppercase">
                        Node ID
                      </div>
                      <div className="mt-0.5 truncate font-mono text-zinc-300">
                        {selectedSession.node_id || "—"}
                      </div>
                    </div>
                  </div>

                  {/* Tool Breakdown Chips */}
                  {Object.keys(selectedSession.tool_breakdown ?? {}).length > 0 && (
                    <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-3">
                      <div className="mb-2 text-[10px] font-semibold tracking-wider text-zinc-400 uppercase">
                        Tool Breakdown ({selectedSession.tool_calls_count} total calls)
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {Object.entries(selectedSession.tool_breakdown).map(
                          ([toolName, count]) => (
                            <span
                              key={toolName}
                              className="inline-flex items-center gap-1.5 rounded-md border border-amber-800/60 bg-amber-950/50 px-2.5 py-1 font-mono text-xs text-amber-300"
                            >
                              <span>{toolName}</span>
                              <span className="rounded bg-amber-900/60 px-1.5 py-0.2 text-[10px] font-bold text-amber-200">
                                {count}
                              </span>
                            </span>
                          ),
                        )}
                      </div>
                    </div>
                  )}

                  {/* Full Message Transcript */}
                  <div className="space-y-3">
                    <div className="text-xs font-semibold tracking-wider text-zinc-400 uppercase">
                      Transcript Messages ({selectedSession.messages?.length ?? 0})
                    </div>
                    {!selectedSession.messages ||
                    selectedSession.messages.length === 0 ? (
                      <div className="rounded-xl border border-zinc-800/70 bg-zinc-900/30 p-6 text-center text-xs text-zinc-500">
                        No transcript messages recorded for this session.
                      </div>
                    ) : (
                      selectedSession.messages.map((msg, idx) => (
                        <TranscriptMessageCard
                          key={idx}
                          message={msg}
                          index={idx}
                        />
                      ))
                    )}
                  </div>
                </>
              ) : null}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
