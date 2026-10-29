import { useEffect, useMemo, useRef, useState } from "react";
import type { AgentState } from "./lib/useAgent";
import type { ActivityEntry, ActivityKind } from "./types";

function logTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

const KIND_LABEL: Record<ActivityKind, string> = {
  task: "task",
  thinking: "brain",
  step: "step",
  answer: "answer",
  confirm: "confirm",
  server: "server",
};

type Filter = "all" | "thinking" | "step" | "task";

const FILTERS: Array<{ id: Filter; label: string }> = [
  { id: "all", label: "All" },
  { id: "thinking", label: "Brain" },
  { id: "step", label: "Steps" },
  { id: "task", label: "Tasks" },
];

export default function Server({ agent }: { agent: AgentState }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
  const api = window.winbrow;

  // The main process keeps only server lifecycle/errors; the brain stream comes
  // straight from the Python side's typed events. Merge both into one timeline.
  const entries = useMemo(() => {
    const server: ActivityEntry[] = agent.connectionLog
      .filter((entry) => entry.kind !== "ping" && entry.source !== "ui")
      .filter(
        (entry) => !/^Sidecar: (WebSocket|connection)/i.test(entry.message),
      )
      .map((entry) => {
        const failed = entry.level === "error";
        return {
          id: `log-${entry.id}`,
          ts: entry.ts,
          kind: "server" as const,
          status: failed
            ? ("failed" as const)
            : entry.level === "warn"
              ? ("running" as const)
              : ("info" as const),
          title: entry.message,
          body: failed ? entry.detail : undefined,
          detail: failed ? undefined : entry.detail,
        };
      });
    const merged = new Map<string, ActivityEntry>();
    for (const entry of [...agent.activity, ...server]) {
      merged.set(entry.id, entry);
    }
    return [...merged.values()].sort((a, b) => a.ts - b.ts);
  }, [agent.activity, agent.connectionLog]);

  const filtered = useMemo(
    () =>
      filter === "all"
        ? entries
        : entries.filter((entry) =>
            filter === "step"
              ? entry.kind === "step"
              : entry.kind === filter,
          ),
    [entries, filter],
  );

  const rows = useMemo(() => {
    let lastTask = "";
    return filtered.map((entry) => {
      const header = Boolean(entry.taskId) && entry.taskId !== lastTask;
      if (entry.taskId) lastTask = entry.taskId;
      return { entry, header };
    });
  }, [filtered]);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const nearBottom =
      node.scrollHeight - node.scrollTop - node.clientHeight < 120;
    if (nearBottom) node.scrollTop = node.scrollHeight;
  }, [rows.length]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const statusClass =
    agent.connection === "online"
      ? "status-completed"
      : agent.connection === "offline"
        ? "status-failed"
        : "status-running";
  const taskCount = Object.keys(agent.tasks).length;
  const stepCount = useMemo(
    () => entries.filter((entry) => entry.kind === "step").length,
    [entries],
  );

  return (
    <div className="activity">
      <div className="task-head connection-head">
        <div className="task-title">
          <span className={`status-text ${statusClass}`}>server</span>
          <h1>Server activity</h1>
          <div className="connection-sub">
            brain plans, reasoning and steps from the Python sidecar ·{" "}
            {taskCount} task{taskCount === 1 ? "" : "s"} · {stepCount} step
            {stepCount === 1 ? "" : "s"}
          </div>
        </div>
        <div className="connection-actions">
          <div className="activity-filters">
            {FILTERS.map((item) => (
              <button
                key={item.id}
                className={`chip ${filter === item.id ? "chip-active" : ""}`}
                onClick={() => setFilter(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button
            className="chip"
            onClick={() => {
              agent.clearActivity();
              agent.clearConnectionLog();
            }}
            title="Clear this view (the log files are kept)"
          >
            clear
          </button>
          <button
            className="btn btn-primary"
            onClick={() => api?.restartSidecar()}
            title="Stop the managed Python server and start it again"
          >
            Restart server
          </button>
        </div>
      </div>

      <div className="connection-scroll" ref={scrollRef}>
        <div className="connection-list">
          {rows.length === 0 ? (
            <div className="connection-empty">
              <span className="pulse" />
              {filter === "all"
                ? "No server activity yet. Start a task and the brain's plans, thinking and steps stream here."
                : `No ${filter} activity yet.`}
            </div>
          ) : (
            rows.map(({ entry, header }) => {
              const task = entry.taskId
                ? agent.tasks[entry.taskId]
                : undefined;
              const open = expanded.has(entry.id);
              const long =
                (entry.body || "").length > 180 ||
                (entry.detail || "").length > 180;
              return (
                <div className="activity-group" key={entry.id}>
                  {header ? (
                    <div className="activity-task">
                      <span
                        className={`activity-dot activity-${task?.status === "failed" ? "failed" : task?.status === "completed" ? "success" : "running"}`}
                      />
                      <span className="activity-task-goal">
                        {task?.goal || entry.title}
                      </span>
                      <span className="activity-task-meta">
                        {task?.status || "running"} · {entry.taskId}
                      </span>
                    </div>
                  ) : null}
                  <div
                    className={`activity-row activity-kind-${entry.kind} ${
                      long ? "activity-clickable" : ""
                    } ${open ? "activity-open" : ""}`}
                    onClick={() => long && toggle(entry.id)}
                    title={long ? "Click to expand" : undefined}
                  >
                    <span className="activity-time">
                      {logTime(entry.ts)}
                    </span>
                    <span
                      className={`activity-kind activity-kind-${entry.kind}`}
                    >
                      {KIND_LABEL[entry.kind]}
                    </span>
                    <span
                      className={`activity-dot activity-${entry.status}`}
                    />
                    <span className="activity-main">
                      <span className="activity-headline">
                        <span className="activity-title">{entry.title}</span>
                        {entry.detail ? (
                          <span className="activity-detail">
                            {entry.detail}
                          </span>
                        ) : null}
                      </span>
                      {entry.body ? (
                        <span
                          className={`activity-body ${
                            open ? "activity-body-open" : ""
                          }`}
                        >
                          {entry.body}
                        </span>
                      ) : null}
                    </span>
                    {entry.elapsedMs ? (
                      <span className="activity-ms">
                        {Math.round(entry.elapsedMs)} ms
                      </span>
                    ) : null}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
