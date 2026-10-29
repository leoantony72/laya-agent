import { useEffect, useMemo, useRef, useState } from "react";
import type { AgentState } from "./lib/useAgent";
import Server from "./Server";
import Trace from "./Trace";
import { SERVER_HTTP } from "./lib/agent";
import { elapsed, relativeTime, shortTime, argHint } from "./lib/format";

const EXAMPLES = [
  "open WordPad and type a short AI document",
  "find the latest invoice on my Desktop and summarize it",
  "download the current weather radar for Kochi",
  "compare these two PDFs and list the differences",
];

function StatusText({ status }: { status: string }) {
  const label =
    status === "completed"
      ? "done"
      : status === "cancelled"
        ? "stopped"
        : status === "needs_input"
          ? "needs input"
          : status;
  return <span className={`status-text status-${status}`}>{label}</span>;
}

function getSuggestedFollowups(task?: any): string[] {
  if (!task || task.status !== "completed") return [];
  const suggestions: string[] = [];
  const goalLower = (task.goal || "").toLowerCase();
  const answerLower = (task.answer || "").toLowerCase();

  if (
    goalLower.includes("file") ||
    goalLower.includes("pdf") ||
    goalLower.includes("doc") ||
    answerLower.includes(".pdf") ||
    answerLower.includes(".rtf") ||
    answerLower.includes(".txt") ||
    answerLower.includes(".html")
  ) {
    suggestions.push("Open the output file");
    suggestions.push("Where was it saved?");
  }
  if (
    goalLower.includes("search") ||
    goalLower.includes("find") ||
    goalLower.includes("summarize") ||
    goalLower.includes("weather")
  ) {
    suggestions.push("Tell me more details");
    suggestions.push("Summarize in 3 bullet points");
  }
  if (suggestions.length < 2) {
    suggestions.push("Explain what you did step by step");
    suggestions.push("What else can you do?");
  }
  return suggestions.slice(0, 4);
}

export default function Dashboard({ agent }: { agent: AgentState }) {
  const [draft, setDraft] = useState("");
  const [rawReasoning, setRawReasoning] = useState(
    () => localStorage.getItem("winbrow.raw") === "1",
  );
  const [sidecar, setSidecar] = useState("online");
  const [voice, setVoice] = useState({ running: false, mode: "", muted: false });
  const [view, setView] = useState<"tasks" | "server">(() =>
    window.location.hash === "#server" || window.location.hash === "#connection"
      ? "server"
      : "tasks",
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const api = window.winbrow;

  const currentSession = agent.activeSession;
  const turns = agent.activeTasks;
  const latestTurn = turns.length > 0 ? turns[turns.length - 1] : agent.active;
  const running = latestTurn?.status === "running";

  const parentTask = useMemo(() => {
    const parentId = latestTurn?.parentId;
    if (!parentId) return null;
    const local = agent.tasks[parentId];
    if (local) return { id: parentId, goal: local.goal || "(untitled task)" };
    const remote = agent.remote.find((task) => task.id === parentId);
    return { id: parentId, goal: remote?.goal || parentId };
  }, [latestTurn?.parentId, agent.tasks, agent.remote]);

  useEffect(() => {
    if (!api?.onMainEvent) return;
    return api.onMainEvent("sidecar:status", (state) => setSidecar(String(state)));
  }, [api]);

  useEffect(() => {
    if (!api?.onMainEvent) return;
    return api.onMainEvent("main:show-server", () => setView("server"));
  }, [api]);

  useEffect(() => {
    let alive = true;
    const load = () => {
      fetch(`${SERVER_HTTP}/api/voice`)
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (alive && data) setVoice(data);
        })
        .catch(() => {});
    };
    load();
    const timer = window.setInterval(load, 4000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const toggleVoice = () => {
    fetch(`${SERVER_HTTP}/api/voice/toggle`, { method: "POST" })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => data && setVoice(data))
      .catch(() => {});
  };

  useEffect(() => {
    const node = scrollRef.current;
    if (!node || turns.length === 0) return;
    node.scrollTop = node.scrollHeight;
  }, [agent.activeSessionId, turns.length, latestTurn?.updatedAt, latestTurn?.answer?.length]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && running) {
        event.preventDefault();
        agent.cancel();
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setView("tasks");
        agent.newChat();
        composerRef.current?.focus();
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        setView("tasks");
        submit();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const chatItems = useMemo(() => {
    return agent.sessionOrder
      .map((id) => agent.sessions[id])
      .filter((s): s is NonNullable<typeof s> => Boolean(s));
  }, [agent.sessionOrder, agent.sessions]);

  function submit(overrideText?: string): void {
    const text = (overrideText ?? draft).trim();
    if (!text) return;
    const parent = latestTurn && !latestTurn.live ? latestTurn.id : "";
    agent.run(text, parent);
    setDraft("");
  }

  function handleOption(option: string, taskId?: string): void {
    agent.run(option, taskId || latestTurn?.id || "");
  }

  function toggleRaw(): void {
    setRawReasoning((value) => {
      localStorage.setItem("winbrow.raw", value ? "0" : "1");
      return !value;
    });
  }

  const liveLabel = agent.connection === "online" ? "connected" : agent.connection;

  return (
    <div className="app">
      <header className="titlebar">
        <div className="brand">
          <span className="brand-mark">◆</span>
          <span className="brand-name">winbrow</span>
          <span className="brand-sub">desktop agent</span>
        </div>
        <div className="view-tabs">
          <button
            className={`view-tab ${view === "tasks" ? "active" : ""}`}
            onClick={() => setView("tasks")}
          >
            Chats
          </button>
          <button
            className={`view-tab ${view === "server" ? "active" : ""}`}
            onClick={() => setView("server")}
            title={`Python server activity (${liveLabel})`}
          >
            <span
              className={`dot dot-${
                agent.connection === "online"
                  ? "success"
                  : agent.connection === "offline"
                    ? "failed"
                    : "running"
              }`}
            />
            Server
          </button>
        </div>
        <div className="titlebar-spacer" />
        <button
          className="chip"
          onClick={toggleRaw}
          title="Show raw provider reasoning when available"
        >
          raw reasoning {rawReasoning ? "on" : "off"}
        </button>
        <div className="window-buttons">
          <button onClick={() => api?.minimize()} title="Minimize">–</button>
          <button onClick={() => api?.maximize()} title="Maximize">□</button>
          <button className="close" onClick={() => api?.closeDashboard()} title="Close">×</button>
        </div>
      </header>

      <div className="layout">
        <aside className="rail">
          <div className="rail-head">
            <span>chats</span>
            <button
              className="rail-new"
              onClick={() => {
                setView("tasks");
                agent.newChat();
                composerRef.current?.focus();
              }}
              title="New chat (Ctrl+K)"
            >
              +
            </button>
          </div>
          <div className="rail-list">
            {chatItems.length === 0 ? (
              <div className="rail-empty">No chats yet.</div>
            ) : (
              chatItems.map((item) => (
                <div
                  key={item.id}
                  className={`rail-item ${item.id === agent.activeSessionId ? "active" : ""}`}
                  onClick={() => {
                    setView("tasks");
                    agent.selectSession(item.id);
                  }}
                >
                  <span
                    className={`dot dot-${
                      item.status === "running"
                        ? "running"
                        : item.status === "needs_input"
                          ? "needs_input"
                          : item.status
                    }`}
                  />
                  <span className="rail-goal">{item.title}</span>
                  <span className="rail-meta">
                    {relativeTime(item.updatedAt || item.createdAt)}
                  </span>
                  <button
                    className="rail-del"
                    title="Delete chat"
                    onClick={(e) => {
                      e.stopPropagation();
                      void agent.deleteChat(item.id);
                    }}
                  >
                    ×
                  </button>
                </div>
              ))
            )}
          </div>
          <div className="rail-foot">
            <div className="rail-stat">
              <span>sidecar</span>
              <button
                className="rail-link"
                onClick={() =>
                  sidecar === "online" ? agent.refresh() : api?.startSidecar()
                }
                title={sidecar === "online" ? "Refresh" : "Start the sidecar"}
              >
                {sidecar}
              </button>
            </div>
            <div className="rail-stat">
              <span>voice</span>
              <button className="rail-link" onClick={toggleVoice} title="Start/stop local voice">
                {voice.running
                  ? voice.muted
                    ? "muted"
                    : voice.mode || "on"
                  : "off"}
              </button>
            </div>
            <div className="rail-stat">
              <span>stop</span>
              <span>Esc</span>
            </div>
          </div>
        </aside>

        <main className="main">
          {view === "server" ? (
            <Server agent={agent} />
          ) : currentSession && turns.length > 0 ? (
            <>
              <div className="task-head">
                <div className="task-title">
                  <StatusText
                    status={
                      latestTurn?.needsInput
                        ? "needs_input"
                        : latestTurn?.status || currentSession.status
                    }
                  />
                  <h1>{currentSession.title || "(untitled chat)"}</h1>
                  {parentTask ? (
                    <button
                      className="task-parent"
                      onClick={() => agent.select(parentTask.id)}
                      title="Open parent task"
                    >
                      ↳ follow-up to {parentTask.goal}
                    </button>
                  ) : null}
                </div>
                <div className="task-meta">
                  <span>
                    {turns.length} message{turns.length === 1 ? "" : "s"}
                  </span>
                  <span>{elapsed(latestTurn || turns[0])}</span>
                  {running ? (
                    <button className="btn btn-stop" onClick={agent.cancel}>
                      Stop
                    </button>
                  ) : null}
                </div>
              </div>
              <div className="trace-scroll" ref={scrollRef}>
                {turns.map((turn) => (
                  <div key={turn.id} className="chat-turn">
                    <div className="turn-user-msg">
                      <span className="turn-user-badge">You</span>
                      <div className="turn-user-text">{turn.goal}</div>
                      <span className="turn-user-meta">{shortTime(turn.createdAt)}</span>
                    </div>
                    <div className="turn-agent-msg">
                      <Trace
                        task={turn}
                        rawReasoning={rawReasoning}
                        onOption={(option) => handleOption(option, turn.id)}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="empty">
              <pre className="empty-logo">{`  ██╗    ██╗██╗███╗   ██╗██████╗ ██████╗  ██████╗ ██╗    ██╗
  ██║    ██║██║████╗  ██║██╔══██╗██╔══██╗██╔═══██╗██║    ██║
  ██║ █╗ ██║██║██╔██╗ ██║██████╔╝██████╔╝██║   ██║██║ █╗ ██║
  ██║███╗██║██║██║╚██╗██║██╔══██╗██╔══██╗██║   ██║██║███╗██║
  ╚███╔███╔╝██║██║ ╚████║██████╔╝██║  ██║╚██████╔╝╚███╔███╔╝
   ╚══╝╚══╝ ╚═╝╚═╝  ╚═══╝╚═════╝ ╚═╝  ╚═╝ ╚═════╝  ╚══╝╚══╝`}</pre>
              <div className="empty-sub">
                Describe a task or command. The agent streams its plan, every tool call,
                and the answer right here.
              </div>
              <div className="empty-examples">
                {EXAMPLES.map((example) => (
                  <button
                    key={example}
                    className="example"
                    onClick={() => {
                      setDraft(example);
                      composerRef.current?.focus();
                    }}
                  >
                    {example}
                  </button>
                ))}
              </div>
            </div>
          )}

          {agent.confirm ? (
            <div className="confirm-bar">
              <div className="confirm-text">
                <strong>{agent.confirm.tier?.toUpperCase()}</strong>{" "}
                {agent.confirm.message || `Allow “${agent.confirm.tool}”?`}
                <span className="confirm-args">{argHint(agent.confirm.args)}</span>
              </div>
              <button className="btn btn-primary" onClick={() => agent.respond(true)}>
                Allow
              </button>
              <button className="btn" onClick={() => agent.respond(false)}>
                Deny
              </button>
            </div>
          ) : null}

          {view === "tasks" ? (
            <div className={`composer ${running ? "composer-busy" : ""}`}>
              {/* Options & Follow-up ribbon */}
              {latestTurn?.needsInput && latestTurn.options && latestTurn.options.length > 0 ? (
                <div className="followup-options-bar">
                  <span className="followup-options-label">Choices:</span>
                  <div className="followup-options-list">
                    {latestTurn.options.map((opt, idx) => (
                      <button
                        key={`${idx}-${opt}`}
                        className="followup-option-chip"
                        onClick={() => handleOption(opt, latestTurn.id)}
                        title={opt}
                      >
                        <span className="chip-idx">{idx + 1}</span>
                        <span className="chip-text">{opt}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : currentSession && turns.length > 0 && latestTurn?.status === "completed" && !running ? (
                <div className="followup-options-bar">
                  <span className="followup-options-label">Suggested follow-ups:</span>
                  <div className="followup-options-list">
                    {getSuggestedFollowups(latestTurn).map((opt, idx) => (
                      <button
                        key={`${idx}-${opt}`}
                        className="followup-option-chip followup-suggestion"
                        onClick={() => handleOption(opt, latestTurn.id)}
                        title={opt}
                      >
                        <span className="chip-text">{opt}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}

              <textarea
                ref={composerRef}
                value={draft}
                rows={1}
                placeholder={
                  running
                    ? "Agent is working… press Esc to stop"
                    : latestTurn?.needsInput
                      ? "Answer the question or select an option above…"
                      : currentSession && turns.length > 0
                        ? "Ask a follow-up or give the next instruction…"
                        : "Ask WinBrow to do something…"
                }
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    submit();
                  }
                }}
                onInput={(event) => {
                  const target = event.currentTarget;
                  target.style.height = "auto";
                  target.style.height = `${Math.min(target.scrollHeight, 200)}px`;
                }}
              />
              <div className="composer-actions">
                <span className="composer-hint">Enter to send · Shift+Enter for a new line</span>
                {running ? (
                  <button className="btn btn-stop" onClick={agent.cancel} style={{ marginRight: 8 }}>
                    Stop
                  </button>
                ) : null}
                <button
                  className="btn btn-primary"
                  onClick={() => submit()}
                  disabled={!draft.trim() && !running}
                >
                  {currentSession && turns.length > 0 ? "Follow up" : "Run"}
                </button>
              </div>
            </div>
          ) : null}
        </main>
      </div>
    </div>
  );
}
