import type React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import type {
  EventRow,
  PlanningMessage,
  PlanningSession,
  PlanningTaskNode,
} from "../types";
import { Badge, Button, Spinner, input } from "../ui";

// Messages longer than this collapse to a few lines with a "Show more" toggle.
const MESSAGE_COLLAPSE_THRESHOLD = 280;

/* ------------------------------------------------------------------------ */
/* Color maps                                                               */
/* ------------------------------------------------------------------------ */

const PLANNING_SESSION_COLORS: Record<string, string> = {
  active: "bg-amber-500",
  stable: "bg-sky-500",
  completed: "bg-emerald-600",
  cancelled: "bg-zinc-500",
};

// Soft text/dot tints for status accents (rings, dots, labels).
const PLANNING_SESSION_ACCENT: Record<
  string,
  { dot: string; text: string; label: string }
> = {
  active: { dot: "bg-amber-400", text: "text-amber-400", label: "Active" },
  stable: { dot: "bg-sky-400", text: "text-sky-400", label: "Stable" },
  completed: {
    dot: "bg-emerald-400",
    text: "text-emerald-400",
    label: "Completed",
  },
  cancelled: { dot: "bg-zinc-500", text: "text-zinc-500", label: "Cancelled" },
};

const PLANNING_NODE_COLORS: Record<string, string> = {
  proposed: "bg-zinc-600",
  refined: "bg-amber-600",
  approved: "bg-emerald-600",
};

// Stable palette assigned to agents by name hash, so each persona keeps its color.
const AGENT_PALETTE: { ring: string; text: string; avatar: string }[] = [
  { ring: "ring-violet-500/40", text: "text-violet-300", avatar: "bg-violet-600" },
  { ring: "ring-sky-500/40", text: "text-sky-300", avatar: "bg-sky-600" },
  { ring: "ring-emerald-500/40", text: "text-emerald-300", avatar: "bg-emerald-600" },
  { ring: "ring-amber-500/40", text: "text-amber-300", avatar: "bg-amber-600" },
  { ring: "ring-rose-500/40", text: "text-rose-300", avatar: "bg-rose-600" },
  { ring: "ring-cyan-500/40", text: "text-cyan-300", avatar: "bg-cyan-600" },
  { ring: "ring-fuchsia-500/40", text: "text-fuchsia-300", avatar: "bg-fuchsia-600" },
  { ring: "ring-teal-500/40", text: "text-teal-300", avatar: "bg-teal-600" },
];

function agentIdentity(name: string) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  const palette = AGENT_PALETTE[Math.abs(h) % AGENT_PALETTE.length];
  const initials =
    name
      .split(/[\s_\-./]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((p) => p[0]?.toUpperCase() ?? "")
      .join("") || name.slice(0, 2).toUpperCase();
  return { ...palette, initials };
}

/* ------------------------------------------------------------------------ */
/* Time helpers                                                             */
/* ------------------------------------------------------------------------ */

function relTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

function clockTime(iso: string): string {
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  return dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* ------------------------------------------------------------------------ */
/* Real-time event stream                                                   */
/* ------------------------------------------------------------------------ */

function usePlanningStream(sessionId: string | null): EventRow[] {
  const [events, setEvents] = useState<EventRow[]>([]);
  useEffect(() => {
    setEvents([]);
    if (!sessionId) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
      `${proto}://${location.host}/ws/planning?session_id=${sessionId}`,
    );
    ws.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as EventRow;
        setEvents((prev) => [...prev.slice(-199), ev]);
      } catch {
        /* ignore malformed messages */
      }
    };
    return () => ws.close();
  }, [sessionId]);
  return events;
}

/* ------------------------------------------------------------------------ */
/* Small presentational atoms                                               */
/* ------------------------------------------------------------------------ */

function SectionHeader({
  children,
  right,
}: {
  children: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
        {children}
      </h3>
      {right}
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const accent = PLANNING_SESSION_ACCENT[status] ?? PLANNING_SESSION_ACCENT.cancelled;
  const pulse = status === "active" ? "animate-pulse" : "";
  return (
    <span
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${accent.dot} ${pulse}`}
      aria-hidden
    />
  );
}

function TypingIndicator() {
  return (
    <div className="flex items-center gap-2 text-xs text-zinc-500">
      <span className="flex gap-1">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-zinc-500 [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-zinc-500 [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-zinc-500" />
      </span>
      agents are thinking…
    </div>
  );
}

/**
 * Long message body that collapses to a few lines by default with a keyboard-
 * accessible "Show more / Show less" toggle. This is the core "wall of text" fix.
 */
function CollapsibleText({
  text,
  className = "",
  toggleClassName = "text-indigo-400 hover:text-indigo-300",
}: {
  text: string;
  className?: string;
  toggleClassName?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > MESSAGE_COLLAPSE_THRESHOLD;

  return (
    <div>
      <div
        className={`whitespace-pre-wrap break-words ${className} ${
          long && !expanded ? "line-clamp-[6]" : ""
        }`}
      >
        {text}
      </div>
      {long && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className={`mt-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500 ${toggleClassName}`}
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

/** Subtle "Turn N" hairline between turn groups in the transcript. */
function TurnDivider({ turn }: { turn: number }) {
  return (
    <div className="flex items-center gap-3 pt-1">
      <span className="text-[11px] uppercase tracking-wide text-zinc-600">
        turn {turn}
      </span>
      <span className="h-px flex-1 bg-zinc-800" />
    </div>
  );
}

function SendIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden="true"
      className="h-4 w-4"
    >
      <path d="M3.105 2.289a.75.75 0 0 0-.826.95l1.414 4.926A1.5 1.5 0 0 0 5.135 9.25h6.115a.75.75 0 0 1 0 1.5H5.135a1.5 1.5 0 0 0-1.442 1.085l-1.414 4.926a.75.75 0 0 0 .826.95 28.897 28.897 0 0 0 15.293-7.155.75.75 0 0 0 0-1.113A28.897 28.897 0 0 0 3.105 2.289Z" />
    </svg>
  );
}

/** Grow a textarea to fit its content, capped by its CSS max-height. */
function autoGrow(el: HTMLTextAreaElement) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
}

/** A stable, ellipsised label for a session (title, else a prompt slice). */
function sessionLabel(s: PlanningSession): string {
  if (s.title?.trim()) return s.title;
  const slice = s.prompt.slice(0, 50);
  return s.prompt.length > 50 ? `${slice}…` : slice;
}

/* ------------------------------------------------------------------------ */
/* Main panel                                                                */
/* ------------------------------------------------------------------------ */

export function AgentCeptionPanel({ projectId }: { projectId: string }) {
  const [sessions, setSessions] = useState<PlanningSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<PlanningMessage[]>([]);
  const [taskNodes, setTaskNodes] = useState<PlanningTaskNode[]>([]);
  const [prompt, setPrompt] = useState("");
  const [title, setTitle] = useState("");
  const [humanInput, setHumanInput] = useState("");

  const [creating, setCreating] = useState(false);
  const [sending, setSending] = useState(false);
  const [approving, setApproving] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [dataLoading, setDataLoading] = useState(false);
  const [error, setError] = useState("");

  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pinnedRef = useRef(true);

  const events = usePlanningStream(activeSessionId);

  const reloadSessions = useCallback(async () => {
    try {
      const ss = await api.listPlanningSessions(projectId);
      setSessions(ss);
    } catch (e) {
      setError(String(e));
    } finally {
      setSessionsLoading(false);
    }
  }, [projectId]);

  const loadSessionData = useCallback(async (sid: string, quiet = false) => {
    if (!quiet) setDataLoading(true);
    try {
      const [msgs, nodes] = await Promise.all([
        api.listPlanningMessages(sid),
        api.listPlanningTaskNodes(sid),
      ]);
      setMessages(msgs);
      setTaskNodes(nodes);
    } catch (e) {
      setError(String(e));
    } finally {
      if (!quiet) setDataLoading(false);
    }
  }, []);

  // Reset selection when switching projects.
  useEffect(() => {
    setActiveSessionId(null);
    setMessages([]);
    setTaskNodes([]);
    setSessionsLoading(true);
  }, [projectId]);

  // Load sessions on mount / project change.
  useEffect(() => {
    reloadSessions();
  }, [reloadSessions]);

  // Auto-select first active/stable session (or first overall) if none selected.
  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      const live = sessions.find(
        (s) => s.status === "active" || s.status === "stable",
      );
      setActiveSessionId(live?.id ?? sessions[0].id);
    }
  }, [sessions, activeSessionId]);

  // Load transcript + tree when the active session changes.
  useEffect(() => {
    if (activeSessionId) {
      pinnedRef.current = true;
      loadSessionData(activeSessionId);
    } else {
      setMessages([]);
      setTaskNodes([]);
    }
  }, [activeSessionId, loadSessionData]);

  // React to real-time planning events.
  useEffect(() => {
    if (!activeSessionId || events.length === 0) return;
    const last = events[events.length - 1];
    if (last.payload?.planning_session_id !== activeSessionId) return;
    const t = last.type;
    if (
      t.startsWith("planning.agent_turn") ||
      t.startsWith("planning.task") ||
      t.startsWith("planning.session_completed")
    ) {
      loadSessionData(activeSessionId, true);
    }
    if (t.includes("created") || t.includes("completed") || t.includes("cancelled")) {
      reloadSessions();
    }
  }, [events, activeSessionId, loadSessionData, reloadSessions]);

  // Polling fallback so the page advances even if the WS drops.
  useEffect(() => {
    const id = setInterval(() => {
      reloadSessions();
      if (activeSessionId) loadSessionData(activeSessionId, true);
    }, 5000);
    return () => clearInterval(id);
  }, [reloadSessions, loadSessionData, activeSessionId]);

  // Track whether the user is pinned to the bottom of the transcript.
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  // Auto-scroll only when pinned to bottom (don't yank the user up).
  useEffect(() => {
    if (pinnedRef.current) {
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages]);

  async function createSession() {
    if (!prompt.trim()) return;
    setCreating(true);
    setError("");
    try {
      const session = await api.createPlanningSession(projectId, {
        title: title.trim() || prompt.trim().slice(0, 60),
        prompt: prompt.trim(),
      });
      setPrompt("");
      setTitle("");
      setActiveSessionId(session.id);
      await reloadSessions();
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  }

  async function sendMessage() {
    if (!humanInput.trim() || !activeSessionId || sending) return;
    const text = humanInput.trim();
    setSending(true);
    setError("");
    try {
      await api.addPlanningMessage(activeSessionId, text);
      setHumanInput("");
      if (inputRef.current) inputRef.current.style.height = "auto";
      pinnedRef.current = true;
      await loadSessionData(activeSessionId, true);
    } catch (e) {
      setError(String(e));
    } finally {
      setSending(false);
    }
  }

  async function approveSession() {
    if (!activeSessionId || approving) return;
    setApproving(true);
    setError("");
    try {
      await api.approvePlanningSession(activeSessionId);
      await Promise.all([
        reloadSessions(),
        loadSessionData(activeSessionId, true),
      ]);
    } catch (e) {
      setError(String(e));
    } finally {
      setApproving(false);
    }
  }

  async function cancelSession() {
    if (!activeSessionId || cancelling) return;
    setCancelling(true);
    setError("");
    try {
      await api.cancelPlanningSession(activeSessionId);
      await Promise.all([
        reloadSessions(),
        loadSessionData(activeSessionId, true),
      ]);
    } catch (e) {
      setError(String(e));
    } finally {
      setCancelling(false);
    }
  }

  const activeSession =
    sessions.find((s) => s.id === activeSessionId) ?? null;
  const isStable = activeSession?.status === "stable";
  const isActive = activeSession?.status === "active";
  const isClosed =
    activeSession?.status === "completed" ||
    activeSession?.status === "cancelled";

  const tree = useMemo(() => buildTree(taskNodes), [taskNodes]);

  const nodeCounts = useMemo(() => {
    const c = { proposed: 0, refined: 0, approved: 0 };
    for (const n of taskNodes) {
      if (n.status in c) c[n.status as keyof typeof c]++;
    }
    return c;
  }, [taskNodes]);

  const lastMsg = messages[messages.length - 1];
  const showTyping = isActive && (!lastMsg || lastMsg.role === "human");

  return (
    <div className="flex flex-col gap-4 lg:grid lg:h-[72vh] lg:grid-cols-[260px_1fr_320px]">
      {/* ----------------------------------------------------------------- */}
      {/* Left: session list + new-session form                            */}
      {/* ----------------------------------------------------------------- */}
      <div className="flex flex-col rounded-xl border border-zinc-800 bg-zinc-900 lg:min-h-0">
        <div className="border-b border-zinc-800 px-3 py-3">
          <SectionHeader
            right={
              <span className="text-xs tabular-nums text-zinc-500">
                {sessions.length}
              </span>
            }
          >
            Sessions
          </SectionHeader>
        </div>

        <div className="space-y-1.5 p-2 lg:flex-1 lg:overflow-y-auto">
          {sessionsLoading && (
            <div className="space-y-1.5">
              {[0, 1, 2].map((i) => (
                <div
                  key={i}
                  className="h-14 animate-pulse rounded-lg bg-zinc-800/50"
                />
              ))}
            </div>
          )}

          {!sessionsLoading && sessions.length === 0 && (
            <div className="px-3 py-10 text-center text-sm text-zinc-500">
              No planning sessions —
              <br />
              start one below.
            </div>
          )}

          {!sessionsLoading &&
            sessions.map((s) => {
              const accent =
                PLANNING_SESSION_ACCENT[s.status] ??
                PLANNING_SESSION_ACCENT.cancelled;
              const selected = activeSessionId === s.id;
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => setActiveSessionId(s.id)}
                  title={s.title || s.prompt}
                  className={`block w-full min-h-[44px] rounded-lg p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500 ${
                    selected
                      ? "bg-zinc-800 ring-1 ring-indigo-500/60"
                      : "hover:bg-zinc-800/50"
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <StatusDot status={s.status} />
                    <span className="flex-1 truncate text-sm font-medium text-zinc-100">
                      {sessionLabel(s)}
                    </span>
                  </div>
                  <div className="mt-1.5 flex items-center justify-between pl-4">
                    <span
                      className={`text-xs font-medium ${accent.text}`}
                    >
                      {accent.label}
                    </span>
                    <span className="text-xs tabular-nums text-zinc-500">
                      turn {s.turn_number}/{s.max_rounds}
                    </span>
                  </div>
                </button>
              );
            })}
        </div>

        {/* New session form */}
        <div className="space-y-2 border-t border-zinc-800 p-3">
          <label
            htmlFor="acp-new-goal"
            className="block text-xs font-medium text-zinc-400"
          >
            New session goal
          </label>
          <textarea
            id="acp-new-goal"
            aria-label="New session goal"
            className={`${input} h-20 resize-none`}
            placeholder="Describe the big goal to decompose…"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                createSession();
              }
            }}
          />
          <input
            className={input}
            aria-label="Session title (optional)"
            placeholder="Title (optional)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <div className="[&>button]:w-full">
            <Button
              variant="primary"
              onClick={createSession}
              disabled={creating || !prompt.trim()}
            >
              {creating ? (
                <span className="flex items-center justify-center gap-2">
                  <Spinner /> Starting…
                </span>
              ) : (
                "Start session"
              )}
            </Button>
          </div>
        </div>
      </div>

      {/* ----------------------------------------------------------------- */}
      {/* Center: discussion transcript                                    */}
      {/* ----------------------------------------------------------------- */}
      <div className="flex flex-col rounded-xl border border-zinc-800 bg-zinc-900 lg:min-h-0 lg:overflow-hidden">
        {/* Header */}
        {activeSession ? (
          <div className="flex flex-col gap-2 border-b border-zinc-800 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex min-w-0 items-center gap-2">
              <StatusDot status={activeSession.status} />
              <span
                className="truncate text-sm font-semibold text-zinc-100"
                title={activeSession.title || activeSession.prompt}
              >
                {sessionLabel(activeSession)}
              </span>
              <Badge
                text={activeSession.status}
                color={
                  PLANNING_SESSION_COLORS[activeSession.status] ?? "bg-zinc-600"
                }
              />
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="mr-auto text-xs tabular-nums text-zinc-500 sm:mr-0">
                turn {activeSession.turn_number}/{activeSession.max_rounds}
              </span>
              {isActive && (
                <Button
                  variant="danger"
                  onClick={cancelSession}
                  disabled={cancelling}
                >
                  {cancelling ? (
                    <span className="flex items-center gap-2">
                      <Spinner /> Cancelling…
                    </span>
                  ) : (
                    "Cancel"
                  )}
                </Button>
              )}
              {isStable && (
                <Button
                  variant="primary"
                  onClick={approveSession}
                  disabled={approving}
                >
                  {approving ? (
                    <span className="flex items-center gap-2">
                      <Spinner /> Approving…
                    </span>
                  ) : (
                    "Approve & create tasks"
                  )}
                </Button>
              )}
            </div>
          </div>
        ) : (
          <div className="border-b border-zinc-800 px-4 py-3">
            <SectionHeader>Discussion</SectionHeader>
          </div>
        )}

        {/* Error banner */}
        {error && (
          <div className="flex items-start justify-between gap-3 border-b border-rose-900/50 bg-rose-950/40 px-4 py-2 text-xs text-rose-300">
            <span className="break-words">{error}</span>
            <button
              type="button"
              onClick={() => setError("")}
              className="shrink-0 text-rose-400 hover:text-rose-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-rose-500"
              title="Dismiss"
            >
              ✕
            </button>
          </div>
        )}

        {/* Messages — capped height on every breakpoint so a long transcript
            stays a scrollable feed rather than a wall of text; fills the column
            at lg+ where the panel becomes a fixed-height 3-column layout. */}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="max-h-[60vh] space-y-4 overflow-y-auto p-4 lg:max-h-none lg:flex-1"
        >
          {!activeSessionId && (
            <div className="flex min-h-[160px] flex-col items-center justify-center gap-2 text-center text-sm text-zinc-500">
              <span className="text-2xl">🪆</span>
              <span>Select a session, or start one to watch agents plan.</span>
            </div>
          )}

          {activeSessionId && dataLoading && messages.length === 0 && (
            <div className="flex min-h-[160px] items-center justify-center gap-2 text-sm text-zinc-500">
              <Spinner /> Loading discussion…
            </div>
          )}

          {activeSessionId &&
            !dataLoading &&
            messages.length === 0 &&
            (isActive ? (
              <div className="flex min-h-[160px] items-center justify-center">
                <TypingIndicator />
              </div>
            ) : (
              <div className="flex min-h-[160px] items-center justify-center text-sm text-zinc-500">
                No messages in this session yet.
              </div>
            ))}

          {messages.map((msg, i) => {
            const prev = messages[i - 1];
            const showTurnDivider =
              msg.role === "agent" &&
              (!prev || prev.turn_number !== msg.turn_number);
            return (
              <div key={msg.id} className="space-y-4">
                {showTurnDivider && <TurnDivider turn={msg.turn_number} />}
                {msg.role === "human" ? (
                  <HumanMessage msg={msg} />
                ) : (
                  <AgentMessage msg={msg} />
                )}
              </div>
            );
          })}

          {showTyping && messages.length > 0 && (
            <div className="pl-11">
              <TypingIndicator />
            </div>
          )}
          <div ref={endRef} />
        </div>

        {/* Input bar */}
        {activeSessionId && (isActive || isStable) && (
          <div className="flex items-end gap-2 border-t border-zinc-800 p-3">
            <textarea
              ref={inputRef}
              aria-label={
                isActive
                  ? "Interject to steer the discussion"
                  : "Add a note before approving"
              }
              className={`${input} max-h-[120px] min-h-[44px] flex-1 resize-none`}
              rows={1}
              placeholder={
                isActive
                  ? "Interject to steer the discussion…  (Enter to send, Shift+Enter for newline)"
                  : "Add a note before approving…"
              }
              value={humanInput}
              disabled={sending}
              onChange={(e) => {
                setHumanInput(e.target.value);
                autoGrow(e.target);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
            />
            <div className="[&>button]:flex [&>button]:min-h-[44px] [&>button]:items-center">
              <Button
                onClick={sendMessage}
                disabled={sending || !humanInput.trim()}
                title="Send message (Enter)"
              >
                {sending ? (
                  <span className="flex items-center gap-2">
                    <Spinner /> Sending…
                  </span>
                ) : (
                  <span className="flex items-center gap-1.5">
                    <SendIcon /> Send
                  </span>
                )}
              </Button>
            </div>
          </div>
        )}
        {activeSessionId && isClosed && (
          <div className="border-t border-zinc-800 px-4 py-2.5 text-center text-xs text-zinc-500">
            This session is {activeSession?.status}. The transcript is read-only.
          </div>
        )}
      </div>

      {/* ----------------------------------------------------------------- */}
      {/* Right: proposed task tree                                        */}
      {/* ----------------------------------------------------------------- */}
      <TaskTreePanel
        tree={tree}
        total={taskNodes.length}
        counts={nodeCounts}
        loading={dataLoading && taskNodes.length === 0}
        hasSession={!!activeSessionId}
        canApprove={isStable}
        approving={approving}
        onApprove={approveSession}
      />
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Transcript messages                                                      */
/* ------------------------------------------------------------------------ */

function AgentMessage({ msg }: { msg: PlanningMessage }) {
  const id = agentIdentity(msg.agent);
  return (
    <div className="flex items-start gap-3">
      <div
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold text-white ${id.avatar}`}
        title={msg.agent}
      >
        {id.initials}
      </div>
      <div
        className={`min-w-0 max-w-[85%] rounded-xl rounded-tl-sm bg-zinc-800 p-3 ring-1 ${id.ring}`}
      >
        <div className="mb-1 flex items-baseline gap-2">
          <span className={`text-xs font-semibold ${id.text}`}>{msg.agent}</span>
          <span className="text-[11px] uppercase tracking-wide text-zinc-500">
            agent
          </span>
          <span className="text-[11px] tabular-nums text-zinc-500">
            turn {msg.turn_number}
          </span>
          <span
            className="text-[11px] text-zinc-500"
            title={new Date(msg.created_at).toLocaleString()}
          >
            {relTime(msg.created_at)}
          </span>
        </div>
        <CollapsibleText
          text={msg.content}
          className="text-sm leading-relaxed text-zinc-100"
        />
      </div>
    </div>
  );
}

function HumanMessage({ msg }: { msg: PlanningMessage }) {
  return (
    <div className="flex items-start justify-end gap-3">
      <div className="min-w-0 max-w-[85%] rounded-xl rounded-tr-sm bg-indigo-600 p-3">
        <div className="mb-1 flex items-baseline justify-end gap-2">
          <span
            className="text-[11px] text-indigo-200"
            title={new Date(msg.created_at).toLocaleString()}
          >
            {clockTime(msg.created_at)}
          </span>
          <span className="text-[11px] uppercase tracking-wide text-indigo-200">
            you
          </span>
        </div>
        <CollapsibleText
          text={msg.content}
          className="text-sm leading-relaxed text-white"
          toggleClassName="text-indigo-200 hover:text-white"
        />
      </div>
      <div
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-indigo-500 text-xs font-semibold text-white"
        title="You (human interjection)"
      >
        You
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Task tree                                                                 */
/* ------------------------------------------------------------------------ */

interface TreeNode extends PlanningTaskNode {
  children: TreeNode[];
}

function buildTree(nodes: PlanningTaskNode[]): TreeNode[] {
  const ordered = [...nodes].sort(
    (a, b) => a.level - b.level || a.sort_order - b.sort_order,
  );
  const map = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];
  for (const n of ordered) map.set(n.id, { ...n, children: [] });
  for (const n of ordered) {
    const node = map.get(n.id)!;
    if (n.parent_id && map.has(n.parent_id)) {
      map.get(n.parent_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

function TaskTreePanel({
  tree,
  total,
  counts,
  loading,
  hasSession,
  canApprove,
  approving,
  onApprove,
}: {
  tree: TreeNode[];
  total: number;
  counts: { proposed: number; refined: number; approved: number };
  loading: boolean;
  hasSession: boolean;
  canApprove: boolean;
  approving: boolean;
  onApprove: () => void;
}) {
  // collapseSignal flips to force every node to a target expanded state.
  const [collapseSignal, setCollapseSignal] = useState<{
    open: boolean;
    n: number;
  }>({ open: true, n: 0 });

  return (
    <div className="flex flex-col rounded-xl border border-zinc-800 bg-zinc-900 lg:min-h-0">
      <div className="space-y-2 border-b border-zinc-800 px-3 py-3">
        <SectionHeader
          right={
            <span className="text-xs tabular-nums text-zinc-500">{total}</span>
          }
        >
          Task tree
        </SectionHeader>
        {total > 0 && (
          <>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
              <LegendItem
                color={PLANNING_NODE_COLORS.proposed}
                label="proposed"
                count={counts.proposed}
              />
              <LegendItem
                color={PLANNING_NODE_COLORS.refined}
                label="refined"
                count={counts.refined}
              />
              <LegendItem
                color={PLANNING_NODE_COLORS.approved}
                label="approved"
                count={counts.approved}
              />
            </div>
            <div className="flex items-center gap-3 text-[11px] text-zinc-500">
              <button
                type="button"
                className="hover:text-zinc-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500"
                onClick={() =>
                  setCollapseSignal((s) => ({ open: true, n: s.n + 1 }))
                }
              >
                Expand all
              </button>
              <button
                type="button"
                className="hover:text-zinc-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500"
                onClick={() =>
                  setCollapseSignal((s) => ({ open: false, n: s.n + 1 }))
                }
              >
                Collapse all
              </button>
            </div>
          </>
        )}
      </div>

      <div className="p-2 lg:max-h-none lg:flex-1 lg:overflow-y-auto">
        {loading && (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-zinc-500">
            <Spinner /> Loading tasks…
          </div>
        )}

        {!loading && !hasSession && (
          <div className="px-3 py-10 text-center text-sm text-zinc-500">
            Start a session to see the proposed decomposition.
          </div>
        )}

        {!loading && hasSession && total === 0 && (
          <div className="px-3 py-10 text-center text-sm text-zinc-500">
            No tasks proposed yet. They appear as agents decompose the goal.
          </div>
        )}

        {!loading &&
          tree.map((node) => (
            <TaskTreeNode
              key={node.id}
              node={node}
              depth={0}
              collapseSignal={collapseSignal}
            />
          ))}
      </div>

      {canApprove && total > 0 && (
        <div className="border-t border-zinc-800 p-3 [&>button]:flex [&>button]:w-full [&>button]:items-center [&>button]:justify-center">
          <Button variant="primary" onClick={onApprove} disabled={approving}>
            {approving ? (
              <span className="flex items-center gap-2">
                <Spinner /> Approving…
              </span>
            ) : (
              `Approve all (${total})`
            )}
          </Button>
        </div>
      )}
    </div>
  );
}

function LegendItem({
  color,
  label,
  count,
}: {
  color: string;
  label: string;
  count: number;
}) {
  return (
    <span className="flex items-center gap-1.5 text-zinc-400">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      {label}
      <span className="tabular-nums text-zinc-500">{count}</span>
    </span>
  );
}

/** Task-node description clamped to 2 lines with a compact expand toggle. */
function NodeDescription({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  // Heuristic: only offer a toggle when the text is long enough to clip.
  const clampable = text.length > 90;
  return (
    <div className="mt-0.5">
      <p
        className={`whitespace-pre-wrap break-words text-xs leading-relaxed text-zinc-400 ${
          clampable && !open ? "line-clamp-2" : ""
        }`}
      >
        {text}
      </p>
      {clampable && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="mt-0.5 text-[11px] font-medium text-indigo-400 hover:text-indigo-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500"
        >
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function TaskTreeNode({
  node,
  depth,
  collapseSignal,
}: {
  node: TreeNode;
  depth: number;
  collapseSignal: { open: boolean; n: number };
}) {
  const [expanded, setExpanded] = useState(true);

  // Respond to expand-all / collapse-all from the header.
  useEffect(() => {
    setExpanded(collapseSignal.open);
  }, [collapseSignal]);

  const hasChildren = node.children.length > 0;
  const statusColor = PLANNING_NODE_COLORS[node.status] ?? "bg-zinc-600";

  return (
    <div>
      <div
        className="group flex items-start gap-1.5 rounded-lg py-1 pr-1 transition-colors hover:bg-zinc-800/50"
        style={{ paddingLeft: `${depth * 14 + 4}px` }}
      >
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          disabled={!hasChildren}
          title={hasChildren ? (expanded ? "Collapse" : "Expand") : undefined}
          className="mt-0.5 w-4 shrink-0 text-left text-xs text-zinc-600 hover:text-zinc-300 disabled:opacity-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500"
        >
          {hasChildren ? (expanded ? "▾" : "▸") : "•"}
        </button>
        <span className="mt-0.5">
          <Badge text={node.status} color={statusColor} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span
              className="truncate text-sm text-zinc-100"
              title={node.title}
            >
              {node.title}
            </span>
            {node.task_id && (
              <span
                className="shrink-0 text-xs text-emerald-400"
                title="Linked to a created task"
              >
                ✓
              </span>
            )}
            <span className="ml-auto shrink-0 text-[10px] tabular-nums text-zinc-600">
              L{node.level}
            </span>
          </div>
          {expanded && node.description && (
            <NodeDescription text={node.description} />
          )}
        </div>
      </div>
      {expanded &&
        node.children.map((child) => (
          <TaskTreeNode
            key={child.id}
            node={child}
            depth={depth + 1}
            collapseSignal={collapseSignal}
          />
        ))}
    </div>
  );
}
