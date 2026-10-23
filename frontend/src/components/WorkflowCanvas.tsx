import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { WorkflowEdge, WorkflowGraph, WorkflowNode } from "../types";
import { NODE_KIND_COLORS } from "../ui";

export interface WorkflowCanvasProps {
  graph: WorkflowGraph;
  selectedNodeId: string | null;
  selectedEdgeId: string | null;
  connectingFromId: string | null;
  nodeStatuses?: Record<
    string,
    "running" | "waiting_for_approval" | "completed" | "failed" | "skipped"
  >;
  armedHitlIds?: string[];
  waitingHitlIds?: string[];
  onSelectNode: (nodeId: string | null) => void;
  onSelectEdge: (edgeId: string | null) => void;
  onMoveNode: (nodeId: string, x: number, y: number) => void;
  onCompleteConnect: (sourceId: string, targetId: string) => void;
  onAutoLayout?: () => void;
}

const NODE_WIDTH = 190;
const PORT_OFFSET_Y = 36;
const NODE_BOTTOM_Y = 74;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 1.6;

/**
 * Compute a clean left-to-right topological layout for a WorkflowGraph,
 * ignoring `on_fail` retry loopback edges when assigning ranks.
 */
export function autoLayoutWorkflow(graph: WorkflowGraph): WorkflowGraph {
  const nodeIds = new Set(graph.nodes.map((n) => n.id));
  const inDegree = new Map<string, number>();
  const adj = new Map<string, string[]>();

  for (const n of graph.nodes) {
    inDegree.set(n.id, 0);
    adj.set(n.id, []);
  }

  for (const e of graph.edges) {
    if (e.condition === "on_fail") continue;
    if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) continue;
    adj.get(e.source)?.push(e.target);
    inDegree.set(e.target, (inDegree.get(e.target) ?? 0) + 1);
  }

  const rank = new Map<string, number>();
  const queue: string[] = [];
  for (const n of graph.nodes) {
    if ((inDegree.get(n.id) ?? 0) === 0) {
      rank.set(n.id, 0);
      queue.push(n.id);
    }
  }

  while (queue.length > 0) {
    const cur = queue.shift()!;
    const curRank = rank.get(cur) ?? 0;
    for (const nxt of adj.get(cur) ?? []) {
      const nextRank = Math.max(rank.get(nxt) ?? 0, curRank + 1);
      rank.set(nxt, nextRank);
      const deg = (inDegree.get(nxt) ?? 1) - 1;
      inDegree.set(nxt, deg);
      if (deg <= 0) {
        queue.push(nxt);
      }
    }
  }

  // Fallback rank for any unvisited nodes
  for (const n of graph.nodes) {
    if (!rank.has(n.id)) {
      rank.set(n.id, 0);
    }
  }

  const columns = new Map<number, WorkflowNode[]>();
  for (const n of graph.nodes) {
    const r = rank.get(n.id) ?? 0;
    const list = columns.get(r) ?? [];
    list.push(n);
    columns.set(r, list);
  }

  const posMap = new Map<string, { x: number; y: number }>();
  const centerY = 220;
  const colSpacingX = 250;
  const rowSpacingY = 112;

  for (const [colIdx, colNodes] of columns.entries()) {
    const x = 64 + colIdx * colSpacingX;
    const count = colNodes.length;
    colNodes.forEach((n, idx) => {
      const offset = (idx - (count - 1) / 2) * rowSpacingY;
      const y = Math.max(36, Math.round(centerY + offset));
      posMap.set(n.id, { x, y });
    });
  }

  return {
    ...graph,
    nodes: graph.nodes.map((n) => {
      const pos = posMap.get(n.id);
      return pos ? { ...n, position_x: pos.x, position_y: pos.y } : n;
    }),
  };
}

interface DragState {
  type: "pan" | "node";
  pointerId: number;
  startClientX: number;
  startClientY: number;
  originX: number;
  originY: number;
  nodeId?: string;
  moved: boolean;
}

const STATUS_STYLES: Record<
  "running" | "waiting_for_approval" | "completed" | "failed" | "skipped",
  { ring: string; dot: string; label: string }
> = {
  running: {
    ring: "ring-2 ring-amber-400/70 shadow-[0_0_16px_rgba(251,191,36,0.22)]",
    dot: "bg-amber-400 animate-pulse",
    label: "Running",
  },
  waiting_for_approval: {
    ring: "ring-2 ring-sky-400/90 shadow-[0_0_20px_rgba(56,189,248,0.35)]",
    dot: "bg-sky-400 animate-ping",
    label: "Waiting for Human Approval",
  },
  completed: {
    ring: "ring-2 ring-emerald-500/70",
    dot: "bg-emerald-400",
    label: "Completed",
  },
  failed: {
    ring: "ring-2 ring-rose-500/80 shadow-[0_0_16px_rgba(244,63,94,0.22)]",
    dot: "bg-rose-400",
    label: "Failed",
  },
  skipped: {
    ring: "ring-1 ring-zinc-700",
    dot: "bg-zinc-500",
    label: "Skipped",
  },
};

export function WorkflowCanvas({
  graph,
  selectedNodeId,
  selectedEdgeId,
  connectingFromId,
  nodeStatuses,
  armedHitlIds,
  waitingHitlIds,
  onSelectNode,
  onSelectEdge,
  onMoveNode,
  onCompleteConnect,
  onAutoLayout,
}: WorkflowCanvasProps) {
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 24, y: 20 });
  const [zoom, setZoom] = useState<number>(1);
  const dragRef = useRef<DragState | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const nodeMap = new Map<string, WorkflowNode>();
  for (const n of graph.nodes) {
    nodeMap.set(n.id, n);
  }

  const clampZoom = useCallback((z: number) => {
    return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(z.toFixed(2))));
  }, []);

  const fitToView = useCallback(() => {
    const el = containerRef.current;
    if (!el || graph.nodes.length === 0) return;
    const cw = el.clientWidth || 720;
    const ch = el.clientHeight || 260;

    // Fit primarily to mandatory / active nodes so optional diff-triggered reviewers
    // don't shrink the main pipeline to tiny text in compact split views.
    const primaryNodes = graph.nodes.filter((n) => !n.optional || n.kind === "planner");
    const targetNodes = primaryNodes.length > 0 ? primaryNodes : graph.nodes;

    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of targetNodes) {
      minX = Math.min(minX, n.position_x);
      minY = Math.min(minY, n.position_y);
      maxX = Math.max(maxX, n.position_x + NODE_WIDTH);
      maxY = Math.max(maxY, n.position_y + NODE_BOTTOM_Y + 24);
    }
    const graphW = Math.max(240, maxX - minX);
    const graphH = Math.max(120, maxY - minY);
    const padX = 28;
    const padTop = 24;
    const padBottom = 38;
    const availW = Math.max(200, cw - padX * 2);
    const availH = Math.max(110, ch - padTop - padBottom);
    const rawScale = Math.min(1, availW / graphW, availH / graphH);
    const scale = Math.min(MAX_ZOOM, Math.max(0.62, Number(rawScale.toFixed(2))));
    const nextPanX = Math.max(12, Math.round((cw - graphW * scale) / 2 - minX * scale));
    const nextPanY = Math.max(
      10,
      Math.round(padTop + (availH - graphH * scale) / 2 - minY * scale),
    );
    setZoom(scale);
    setPan({ x: nextPanX, y: nextPanY });
  }, [graph.nodes]);

  const fittedGraphKeyRef = useRef<string>("");
  useEffect(() => {
    const key = `${graph.name}:${graph.nodes.map((n) => n.id).join(",")}`;
    if (fittedGraphKeyRef.current !== key) {
      fittedGraphKeyRef.current = key;
      fitToView();
    }
  }, [graph.name, graph.nodes, fitToView]);

  function handleWheel(e: React.WheelEvent<HTMLDivElement>) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const delta = e.deltaY < 0 ? 0.1 : -0.1;
      setZoom((z) => clampZoom(z + delta));
    }
  }

  function handleCanvasPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    // Only pan when clicking the background itself (not a node, edge, or floating control)
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest("[data-canvas-interactive='true']")) return;

    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      type: "pan",
      pointerId: e.pointerId,
      startClientX: e.clientX,
      startClientY: e.clientY,
      originX: pan.x,
      originY: pan.y,
      moved: false,
    };
  }

  function handleCanvasPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;

    const dx = e.clientX - drag.startClientX;
    const dy = e.clientY - drag.startClientY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
      drag.moved = true;
    }

    if (drag.type === "pan") {
      setPan({
        x: Math.round(drag.originX + dx),
        y: Math.round(drag.originY + dy),
      });
    } else if (drag.type === "node" && drag.nodeId) {
      const worldDx = dx / zoom;
      const worldDy = dy / zoom;
      const nextX = Math.round((drag.originX + worldDx) / 4) * 4;
      const nextY = Math.round((drag.originY + worldDy) / 4) * 4;
      onMoveNode(drag.nodeId, nextX, nextY);
    }
  }

  function handleCanvasPointerUp(e: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }

    if (drag.type === "pan" && !drag.moved) {
      onSelectNode(null);
      onSelectEdge(null);
    }
    dragRef.current = null;
  }

  function startNodeDrag(e: React.PointerEvent<HTMLDivElement>, node: WorkflowNode) {
    e.stopPropagation();
    if (e.button !== 0) return;

    // If connecting mode is active and user clicks a different node, complete connection!
    if (connectingFromId && connectingFromId !== node.id) {
      onCompleteConnect(connectingFromId, node.id);
      return;
    }

    onSelectNode(node.id);
    onSelectEdge(null);

    const container = containerRef.current;
    if (container) {
      container.setPointerCapture(e.pointerId);
    }
    dragRef.current = {
      type: "node",
      pointerId: e.pointerId,
      startClientX: e.clientX,
      startClientY: e.clientY,
      originX: node.position_x,
      originY: node.position_y,
      nodeId: node.id,
      moved: false,
    };
  }

  function computeEdgeGeometry(edge: WorkflowEdge): {
    path: string;
    labelX: number;
    labelY: number;
    isRetry: boolean;
  } | null {
    const source = nodeMap.get(edge.source);
    const target = nodeMap.get(edge.target);
    if (!source || !target) return null;

    if (edge.condition === "on_fail") {
      const sx = source.position_x + NODE_WIDTH / 2;
      const sy = source.position_y + NODE_BOTTOM_Y;
      const tx = target.position_x + NODE_WIDTH / 2;
      const ty = target.position_y + NODE_BOTTOM_Y;
      const arcDepth = Math.max(58, Math.min(120, Math.abs(sx - tx) * 0.22 + 52));
      const loopY = Math.max(sy, ty) + arcDepth;
      const path = `M ${sx} ${sy} C ${sx} ${loopY}, ${tx} ${loopY}, ${tx} ${ty}`;
      const labelX = (sx + tx) / 2;
      const labelY = loopY - 12;
      return { path, labelX, labelY, isRetry: true };
    }

    const sx = source.position_x + NODE_WIDTH;
    const sy = source.position_y + PORT_OFFSET_Y;
    const tx = target.position_x;
    const ty = target.position_y + PORT_OFFSET_Y;
    const dx = Math.max(64, Math.abs(tx - sx) * 0.45);
    const path = `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`;
    const labelX = (sx + tx) / 2;
    const labelY = (sy + ty) / 2 - 10;
    return { path, labelX, labelY, isRetry: false };
  }

  function edgeStyle(edge: WorkflowEdge, isSelected: boolean): {
    stroke: string;
    dashArray?: string;
    markerEnd: string;
    strokeWidth: number;
  } {
    if (edge.condition === "on_fail") {
      return {
        stroke: isSelected ? "#fb7185" : "#f43f5e",
        dashArray: "6,4",
        markerEnd: "url(#arrow-rose)",
        strokeWidth: isSelected ? 2.6 : 1.9,
      };
    }
    if (edge.condition === "on_diff_trigger") {
      return {
        stroke: isSelected ? "#c084fc" : "#a855f7",
        dashArray: "5,4",
        markerEnd: "url(#arrow-violet)",
        strokeWidth: isSelected ? 2.6 : 1.9,
      };
    }
    if (edge.condition === "on_pass") {
      return {
        stroke: isSelected ? "#818cf8" : "#6366f1",
        markerEnd: "url(#arrow-indigo)",
        strokeWidth: isSelected ? 2.6 : 1.9,
      };
    }
    return {
      stroke: isSelected ? "#818cf8" : "#71717a",
      markerEnd: isSelected ? "url(#arrow-indigo)" : "url(#arrow-zinc)",
      strokeWidth: isSelected ? 2.6 : 1.8,
    };
  }

  return (
    <div
      ref={containerRef}
      onWheel={handleWheel}
      onPointerDown={handleCanvasPointerDown}
      onPointerMove={handleCanvasPointerMove}
      onPointerUp={handleCanvasPointerUp}
      style={{
        backgroundImage: "radial-gradient(#27272a 1px, transparent 1px)",
        backgroundSize: "20px 20px",
        backgroundPosition: `${pan.x}px ${pan.y}px`,
      }}
      className="relative h-full w-full flex-1 select-none overflow-hidden rounded-xl border border-zinc-800/90 bg-zinc-950 cursor-grab active:cursor-grabbing"
    >
      {/* Connection mode banner */}
      {connectingFromId && (
        <div
          data-canvas-interactive="true"
          className="pointer-events-auto absolute top-3 left-1/2 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full border border-indigo-500/50 bg-indigo-950/90 px-3.5 py-1 text-xs text-indigo-200 shadow-lg backdrop-blur-sm"
        >
          <span className="h-2 w-2 animate-ping rounded-full bg-indigo-400" />
          <span>
            Connecting from <strong className="font-mono">{connectingFromId}</strong> — click any target node
          </span>
        </div>
      )}

      {/* Transformed World Layer (SVG Edges + HTML Nodes) */}
      <div
        style={{
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: "0 0",
        }}
        className="pointer-events-none absolute inset-0 h-full w-full"
      >
        {/* SVG Edges Layer */}
        <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible">
          <defs>
            <marker
              id="arrow-zinc"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 8.5 5 L 0 8.5 z" fill="#71717a" />
            </marker>
            <marker
              id="arrow-indigo"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 8.5 5 L 0 8.5 z" fill="#6366f1" />
            </marker>
            <marker
              id="arrow-violet"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 8.5 5 L 0 8.5 z" fill="#a855f7" />
            </marker>
            <marker
              id="arrow-rose"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 8.5 5 L 0 8.5 z" fill="#f43f5e" />
            </marker>
          </defs>

          {graph.edges.map((edge) => {
            const geom = computeEdgeGeometry(edge);
            if (!geom) return null;
            const isSelected = selectedEdgeId === edge.id;
            const style = edgeStyle(edge, isSelected);
            const isHitlWaiting = Boolean(
              waitingHitlIds?.includes(edge.id) ||
                (waitingHitlIds?.includes(edge.source) &&
                  (edge.require_human || armedHitlIds?.includes(edge.id))),
            );
            const isHitlArmed = Boolean(
              edge.require_human || armedHitlIds?.includes(edge.id),
            );

            return (
              <g
                key={edge.id}
                data-canvas-interactive="true"
                className="pointer-events-auto cursor-pointer"
                onPointerDown={(e) => {
                  e.stopPropagation();
                  onSelectEdge(edge.id);
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onSelectEdge(edge.id);
                }}
              >
                {/* Wide transparent hit target for easy clicking */}
                <path
                  d={geom.path}
                  fill="none"
                  stroke="transparent"
                  strokeWidth={16}
                />
                {/* Selection or HITL waiting glow */}
                {(isSelected || isHitlWaiting) && (
                  <path
                    d={geom.path}
                    fill="none"
                    stroke={isHitlWaiting ? "#38bdf8" : style.stroke}
                    strokeWidth={7}
                    strokeOpacity={0.28}
                  />
                )}
                {/* Visible edge curve */}
                <path
                  d={geom.path}
                  fill="none"
                  stroke={isHitlWaiting ? "#38bdf8" : isHitlArmed ? "#0ea5e9" : style.stroke}
                  strokeWidth={isHitlWaiting ? 2.8 : style.strokeWidth}
                  strokeDasharray={style.dashArray}
                  markerEnd={style.markerEnd}
                  className="transition-colors"
                />
              </g>
            );
          })}
        </svg>

        {/* HTML Interactive Edge / HITL Connector Pills Layer */}
        {graph.edges.map((edge) => {
          const geom = computeEdgeGeometry(edge);
          if (!geom) return null;
          const isSelected = selectedEdgeId === edge.id;
          const isHitlWaiting = Boolean(
            waitingHitlIds?.includes(edge.id) ||
              (waitingHitlIds?.includes(edge.source) &&
                (edge.require_human || armedHitlIds?.includes(edge.id))),
          );
          const isHitlArmed = Boolean(
            edge.require_human || armedHitlIds?.includes(edge.id),
          );
          const baseBadge =
            edge.condition === "on_fail"
              ? edge.label || `retry ≤${edge.max_iterations}x`
              : edge.label ||
                (edge.condition === "on_diff_trigger" ? "diff trigger" : "HITL");
          const badgeText = isHitlWaiting
            ? "⏸ Awaiting Human"
            : isHitlArmed
              ? `👤 ${edge.label || "HITL"}`
              : baseBadge;

          return (
            <button
              key={`pill-${edge.id}`}
              type="button"
              data-canvas-interactive="true"
              data-edge-id={edge.id}
              title={`Connector ${edge.source} → ${edge.target} (${edge.condition}) — click to inspect, participate, or pause`}
              onPointerDown={(e) => {
                e.stopPropagation();
              }}
              onClick={(e) => {
                e.stopPropagation();
                onSelectEdge(edge.id);
              }}
              style={{
                left: geom.labelX,
                top: geom.labelY,
                transform: "translate(-50%, -50%)",
              }}
              className={`pointer-events-auto absolute z-10 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[9.5px] font-medium whitespace-nowrap shadow-sm transition-all ${
                isSelected
                  ? "border-indigo-400 bg-indigo-950 text-indigo-200 ring-2 ring-indigo-400/60"
                  : isHitlWaiting
                    ? "border-sky-400 bg-sky-950 text-sky-200 ring-2 ring-sky-400/70 animate-pulse"
                    : isHitlArmed
                      ? "border-sky-600/80 bg-sky-950/90 text-sky-200 hover:border-sky-400"
                      : geom.isRetry
                        ? "border-rose-700/80 bg-rose-950/90 text-rose-200 hover:border-rose-500"
                        : "border-zinc-700 bg-zinc-900/95 text-zinc-300 hover:border-indigo-400 hover:text-white"
              }`}
            >
              {badgeText}
            </button>
          );
        })}

        {/* HTML Nodes Layer */}
        {graph.nodes.map((node) => {
          const kindPalette =
            NODE_KIND_COLORS[node.kind] ?? NODE_KIND_COLORS.custom;
          const isSelected = selectedNodeId === node.id;
          const isConnectSource = connectingFromId === node.id;
          const isConnectTarget =
            connectingFromId !== null && connectingFromId !== node.id;
          const status = nodeStatuses?.[node.id];
          const statusStyle = status ? STATUS_STYLES[status] : null;

          return (
            <div
              key={node.id}
              data-canvas-interactive="true"
              onPointerDown={(e) => startNodeDrag(e, node)}
              style={{
                width: NODE_WIDTH,
                transform: `translate(${node.position_x}px, ${node.position_y}px)`,
              }}
              className={`pointer-events-auto absolute top-0 left-0 rounded-xl border bg-zinc-900/95 p-2.5 shadow-md backdrop-blur-sm transition-shadow ${
                kindPalette.border
              } ${!node.enabled ? "opacity-50 saturate-50" : ""} ${
                isSelected
                  ? "ring-2 ring-indigo-400 shadow-[0_0_22px_rgba(99,102,241,0.28)]"
                  : statusStyle
                    ? statusStyle.ring
                    : "hover:border-zinc-600"
              } ${
                isConnectSource
                  ? "ring-2 ring-indigo-400 animate-pulse"
                  : isConnectTarget
                    ? "cursor-crosshair hover:ring-2 hover:ring-emerald-400/80"
                    : "cursor-grab active:cursor-grabbing"
              }`}
            >
              {/* Input Port (left circle) */}
              <button
                type="button"
                title={
                  isConnectTarget
                    ? `Connect ${connectingFromId} → ${node.id}`
                    : `Input port (${node.id})`
                }
                onPointerDown={(e) => {
                  e.stopPropagation();
                  if (connectingFromId && connectingFromId !== node.id) {
                    onCompleteConnect(connectingFromId, node.id);
                  }
                }}
                style={{ top: PORT_OFFSET_Y - 6 }}
                className={`absolute -left-1.5 h-3 w-3 rounded-full border transition-transform ${
                  isConnectTarget
                    ? "scale-125 border-emerald-300 bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]"
                    : "border-zinc-600 bg-zinc-900 hover:scale-125 hover:border-indigo-400"
                }`}
              />

              {/* Output Port (right circle) */}
              <div
                title={`Output port (${node.id})`}
                style={{ top: PORT_OFFSET_Y - 6 }}
                className={`absolute -right-1.5 h-3 w-3 rounded-full border transition-transform ${
                  isConnectSource
                    ? "scale-125 border-indigo-300 bg-indigo-500 shadow-[0_0_8px_rgba(99,102,241,0.8)]"
                    : "border-zinc-600 bg-zinc-900"
                }`}
              />

              {/* Top row: Kind badge + optional/triggers/disabled + live status dot */}
              <div className="mb-1.5 flex items-center justify-between gap-1">
                <span
                  className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${kindPalette.badge}`}
                >
                  <span className={`h-1.5 w-1.5 rounded-full ${kindPalette.dot}`} />
                  {node.kind}
                </span>

                <div className="flex items-center gap-1">
                  {status === "waiting_for_approval" && (
                    <span className="rounded border border-sky-500/70 bg-sky-950/90 px-1.5 py-0.5 text-[9px] font-semibold text-sky-300">
                      ⏸ Awaiting Human
                    </span>
                  )}
                  {status !== "waiting_for_approval" &&
                    (node.pause_after ||
                      node.await_human_approval ||
                      armedHitlIds?.includes(node.id)) && (
                      <span
                        title="Human-in-the-Loop pause enabled after this node"
                        className="rounded border border-sky-800/70 bg-sky-950/70 px-1 py-0.5 text-[9px] font-medium text-sky-300"
                      >
                        👤 HITL
                      </span>
                    )}
                  {statusStyle && (
                    <span
                      title={`Status: ${statusStyle.label}`}
                      className={`h-2 w-2 rounded-full ${statusStyle.dot}`}
                    />
                  )}
                  {!node.enabled ? (
                    <span className="rounded bg-zinc-800 px-1 py-0.5 text-[9px] font-medium text-zinc-500">
                      off
                    </span>
                  ) : node.triggers.length > 0 ? (
                    <span
                      title={`Triggers: ${node.triggers.join(", ")}`}
                      className="rounded border border-amber-800/60 bg-amber-950/70 px-1 py-0.5 text-[9px] font-medium text-amber-300"
                    >
                      ⚡ {node.triggers.length}{" "}
                      {node.triggers.length === 1 ? "trigger" : "triggers"}
                    </span>
                  ) : node.optional ? (
                    <span className="rounded border border-zinc-700/80 bg-zinc-800/80 px-1 py-0.5 text-[9px] text-zinc-400">
                      optional
                    </span>
                  ) : null}
                </div>
              </div>

              {/* Node Title */}
              <div className="truncate text-xs font-semibold text-zinc-100">
                {node.label || node.id}
              </div>

              {/* Agent Persona & Output Contract Footer */}
              <div className="mt-1 flex items-center justify-between gap-1 text-[11px] text-zinc-400">
                <span className="truncate font-mono text-[10px] text-zinc-400">
                  {node.agent ? `@${node.agent}` : `stage:${node.kind}`}
                </span>
                <span className="shrink-0 rounded bg-zinc-800/90 px-1 py-0.2 font-mono text-[9px] text-zinc-500">
                  {node.output_contract}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Bottom-left Floating Zoom & Auto-Layout Controls */}
      <div
        data-canvas-interactive="true"
        className="pointer-events-auto absolute bottom-3 left-3 z-20 flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900/90 p-1 shadow-lg backdrop-blur-sm"
      >
        <button
          type="button"
          onClick={() => setZoom((z) => clampZoom(z - 0.15))}
          title="Zoom out"
          className="flex h-6 w-6 items-center justify-center rounded text-xs font-semibold text-zinc-300 transition-colors hover:bg-zinc-800"
        >
          −
        </button>
        <button
          type="button"
          onClick={fitToView}
          title="Fit workflow to canvas"
          className="px-2 py-0.5 font-mono text-[11px] text-zinc-300 transition-colors hover:bg-zinc-800 rounded"
        >
          {Math.round(zoom * 100)}% · Fit
        </button>
        <button
          type="button"
          onClick={() => setZoom((z) => clampZoom(z + 0.15))}
          title="Zoom in"
          className="flex h-6 w-6 items-center justify-center rounded text-xs font-semibold text-zinc-300 transition-colors hover:bg-zinc-800"
        >
          +
        </button>
        {onAutoLayout && (
          <>
            <span className="mx-0.5 h-3.5 w-px bg-zinc-800" />
            <button
              type="button"
              onClick={onAutoLayout}
              title="Tidy node positions automatically"
              className="rounded px-2 py-0.5 text-[11px] font-medium text-zinc-300 transition-colors hover:bg-zinc-800 hover:text-white"
            >
              Auto-Layout
            </button>
          </>
        )}
      </div>

      {/* Bottom-right Compact Edge Legend */}
      <div
        data-canvas-interactive="true"
        className="pointer-events-auto absolute right-3 bottom-3 z-20 hidden items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900/90 px-2.5 py-1.5 text-[10px] text-zinc-400 shadow-lg backdrop-blur-sm sm:flex"
      >
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-3.5 bg-indigo-500" />
          Solid: Always/Pass
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-3.5 border-b border-dashed border-purple-400" />
          Dashed: Diff Trigger
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-3.5 border-b border-dashed border-rose-500" />
          Rose Loop: Retry on Fail
        </span>
      </div>
    </div>
  );
}
