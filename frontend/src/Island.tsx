import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { AgentState } from "./lib/useAgent";
import type { IslandPhase, MicState } from "./lib/islandState";
import { deriveIsland } from "./lib/islandState";
import { argHint, skillVerb } from "./lib/format";

const AUTO_COLLAPSE_MS = 8000;

function MicIcon({ mic }: { mic: MicState }) {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden>
      <path
        d="M8 1.6a2.1 2.1 0 0 1 2.1 2.1v3.4a2.1 2.1 0 1 1-4.2 0V3.7A2.1 2.1 0 0 1 8 1.6Z"
        fill="currentColor"
      />
      <path
        d="M3.8 7.2v.6a4.2 4.2 0 0 0 8.4 0v-.6M8 12v2.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
      {mic === "muted" || mic === "off" ? (
        <path
          d="M2.2 13.8 13.8 2.2"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
      ) : null}
    </svg>
  );
}

function PhaseIcon({ phase }: { phase: IslandPhase }) {
  if (phase === "working" || phase === "thinking") {
    return <span className="phase-spinner" aria-hidden />;
  }
  if (phase === "speaking") {
    return (
      <span className="phase-speak" aria-hidden>
        <i />
        <i />
        <i />
      </span>
    );
  }
  if (phase === "done") {
    return (
      <span className="phase-check" aria-hidden>
        ✓
      </span>
    );
  }
  if (phase === "error") {
    return (
      <span className="phase-alert" aria-hidden>
        !
      </span>
    );
  }
  if (phase === "confirm") {
    return (
      <span className="phase-alert" aria-hidden>
        ?
      </span>
    );
  }
  return null;
}

function Waveform({ level }: { level: number }) {
  const bars = [0.35, 0.62, 0.9, 0.52, 0.78, 0.44, 0.66];
  return (
    <span
      className="waveform"
      style={{ ["--level" as string]: Math.max(0.12, Math.min(1, level)) }}
      aria-hidden
    >
      {bars.map((base, index) => (
        <i
          key={index}
          style={{
            animationDelay: `${index * 0.09}s`,
            ["--base" as string]: base,
          }}
        />
      ))}
    </span>
  );
}

function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export default function Island({ agent }: { agent: AgentState }) {
  const [expanded, setExpanded] = useState(
    () => new URLSearchParams(location.search).get("open") === "1",
  );
  const [flash, setFlash] = useState("");
  const [sidecarError, setSidecarError] = useState("");
  const [draft, setDraft] = useState("");
  const [now, setNow] = useState(Date.now());
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const lastInteraction = useRef(Date.now());
  const flashTimer = useRef<number | null>(null);
  const blurTimer = useRef<number | null>(null);
  const agentRef = useRef(agent);
  agentRef.current = agent;
  const api = window.winbrow;

  const view = deriveIsland(agent, now, expanded, flash);
  const refreshContext = agent.refreshContext;
  const dismissConfirm = agent.dismissConfirm;
  const connected = agent.connection === "online";
  const confirm = agent.confirm;

  const touch = () => {
    lastInteraction.current = Date.now();
  };

  const showFlash = (message: string, ms = 2200) => {
    setFlash(message);
    setExpanded(true);
    touch();
    if (flashTimer.current) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => {
      setFlash("");
      flashTimer.current = null;
    }, ms);
  };

  // Keep the transparent Electron window exactly the size of the pill/card.
  useLayoutEffect(() => {
    const node = rootRef.current;
    if (!node) return;
    const send = () => api?.resizeIsland(node.offsetWidth, node.offsetHeight);
    const observer = new ResizeObserver(send);
    observer.observe(node);
    send();
    return () => observer.disconnect();
  }, [api, expanded]);

  // Click-through is owned by the main process (cursor hit-test against the
  // window bounds), so it works even while the window is ignoring mouse
  // events. The renderer only tracks activity for auto-collapse.

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  // A permission prompt escalates: open the card and stay open.
  useEffect(() => {
    if (!confirm) return;
    setExpanded(true);
    touch();
  }, [confirm?.confirmationId]);

  // Auto-collapse after inactivity, but never while a decision is pending.
  useEffect(() => {
    if (!expanded || view.mustStayOpen) return;
    const timer = window.setInterval(() => {
      if (
        Date.now() - lastInteraction.current > AUTO_COLLAPSE_MS &&
        !draft.trim()
      ) {
        setExpanded(false);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [expanded, view.mustStayOpen, draft]);

  useEffect(() => {
    touch();
  }, [view.phase, agent.voice.partial, agent.voice.spoken, agent.active?.updatedAt]);

  // Context (active app / window) is only useful in the card or the control
  // banner; do not scan the desktop while the idle pill is showing.
  useEffect(() => {
    if (!expanded && view.phase !== "control") return;
    refreshContext();
    const timer = window.setInterval(refreshContext, 10_000);
    return () => window.clearInterval(timer);
  }, [expanded, view.phase, refreshContext]);

  useEffect(() => {
    if (!api?.onMainEvent) return;
    const offToggle = api.onMainEvent("main:toggle-island", () => {
      setExpanded((value) => !value);
      touch();
    });
    const offKill = api.onMainEvent("main:kill", () => {
      agentRef.current.cancel();
      void agentRef.current.voiceAction("interrupt");
      showFlash("Stopped");
    });
    const offAlert = api.onMainEvent("main:alert", (payload) => {
      showFlash(String(payload?.message || "Alert"));
    });
    const offSidecarError = api.onMainEvent("sidecar:error", (payload) => {
      setSidecarError(String(payload?.message || "Sidecar unavailable"));
      setExpanded(true);
      touch();
    });
    return () => {
      offToggle();
      offKill();
      offAlert();
      offSidecarError();
    };
  }, [api]);

  useEffect(() => {
    return () => {
      if (flashTimer.current) window.clearTimeout(flashTimer.current);
      if (blurTimer.current) window.clearTimeout(blurTimer.current);
    };
  }, []);

  // Expired confirmations can no longer be approved by the sidecar.
  useEffect(() => {
    if (!confirm?.expiresAt) return;
    if (now <= confirm.expiresAt + 1000) return;
    dismissConfirm();
    showFlash("Request expired");
  }, [now, confirm, dismissConfirm]);

  const openDashboard = () => {
    api?.openDashboard();
    setExpanded(false);
  };

  const kill = () => {
    agent.cancel();
    void agent.voiceAction("interrupt");
    showFlash("Stopped");
  };

  const toggleMic = () => {
    if (!connected) {
      showFlash("Sidecar offline");
      return;
    }
    if (view.mic === "muted") void agent.voiceAction("unmute");
    else if (view.mic === "listening" || view.mic === "on")
      void agent.voiceAction("mute");
    else void agent.voiceAction("start-wake");
  };

  const runCommand = (command: string) => {
    if (!connected) {
      showFlash("Sidecar offline");
      return;
    }
    agent.run(command);
    setExpanded(false);
  };

  const send = () => {
    const text = draft.trim();
    if (!text) return;
    runCommand(text);
    setDraft("");
  };

  const quick = (command: string) => runCommand(command);

  const ptt = () => {
    if (!connected) {
      showFlash("Sidecar offline");
      return;
    }
    void agent.voiceAction("ptt");
    touch();
    if (inputRef.current) inputRef.current.blur();
  };

  const expiresIn =
    confirm?.expiresAt && now < confirm.expiresAt
      ? formatCountdown(confirm.expiresAt - now)
      : "";

  const micTitle =
    view.mic === "muted"
      ? "Microphone muted — click to unmute"
      : view.mic === "listening"
        ? "Microphone listening — click to mute"
        : view.mic === "on"
          ? "Microphone on — click to mute"
          : "Voice is off — click to start listening";

  const phaseClass = `state-${view.phase}`;
  const sizeClass = `size-${expanded ? "large" : view.size}`;

  return (
    <div
      className="island-root"
      ref={rootRef}
      onMouseEnter={touch}
      onMouseMove={touch}
    >
      <motion.div
        layout
        className={`island ${sizeClass} ${phaseClass} ${
          expanded ? "island-expanded" : ""
        }`}
        transition={{ type: "spring", stiffness: 460, damping: 36 }}
        data-phase={view.phase}
      >
        {view.phase === "control" ? (
          <div className="island-control">
            <span className="control-beacon" aria-hidden />
            <div className="control-copy">
              <span className="control-title">
                Agent is controlling your computer
              </span>
              <span className="control-target">{view.controlTarget}</span>
            </div>
            <button
              className="kill-switch"
              onClick={kill}
              title="Stop the agent (Ctrl+Alt+Esc)"
            >
              Stop
            </button>
          </div>
        ) : null}

        <div
          className="island-bar"
          onClick={() => {
            setExpanded((value) => !value);
            touch();
          }}
        >
          <button
            className={`mic mic-${view.mic}`}
            onClick={(event) => {
              event.stopPropagation();
              toggleMic();
            }}
            title={micTitle}
            aria-label={micTitle}
          >
            <MicIcon mic={view.mic} />
          </button>

          {view.phase !== "control" ? (
            <span className={`island-status status-${view.phase}`}>
              {view.status}
            </span>
          ) : null}

          {view.phase === "listening" ? (
            <Waveform level={agent.voice.level} />
          ) : (
            <PhaseIcon phase={view.phase} />
          )}

          {view.detail &&
          view.phase !== "idle" &&
          view.phase !== "control" ? (
            <span className="island-detail" title={view.detail}>
              {view.detail}
            </span>
          ) : null}

          {view.badge > 0 ? (
            <span
              className="island-badge"
              title={`${view.badge} background task${view.badge === 1 ? "" : "s"}`}
            >
              {view.badge}
            </span>
          ) : null}

          <span className="island-spacer" />

          {view.phase === "working" && view.stepTotal > 0 ? (
            <span className="island-step" title="Step progress">
              {view.stepIndex}/{view.stepTotal}
            </span>
          ) : null}

          {view.elapsedLabel ? (
            <span className="island-clock">{view.elapsedLabel}</span>
          ) : null}

          {view.canCancel && view.phase !== "control" ? (
            <button
              className="island-stop"
              title="Stop the current task (Ctrl+Alt+Esc)"
              aria-label="Stop the current task"
              onClick={(event) => {
                event.stopPropagation();
                kill();
              }}
            >
              <svg viewBox="0 0 10 10" width="8" height="8" aria-hidden>
                <rect x="1" y="1" width="8" height="8" rx="1.6" fill="currentColor" />
              </svg>
            </button>
          ) : null}

          {(view.phase === "done" || view.phase === "error") && !expanded ? (
            <button
              className="island-open"
              title="Open the dashboard"
              aria-label="Open the dashboard"
              onClick={(event) => {
                event.stopPropagation();
                openDashboard();
              }}
            >
              <svg viewBox="0 0 10 10" width="9" height="9" aria-hidden>
                <path
                  d="M4 2h4v4M8 2 3 7"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          ) : null}

          <button
            className={`island-chev ${expanded ? "open" : ""}`}
            title={expanded ? "Collapse (Ctrl+Alt+I)" : "Expand (Ctrl+Alt+I)"}
            aria-label={expanded ? "Collapse island" : "Expand island"}
            onClick={(event) => {
              event.stopPropagation();
              setExpanded((value) => !value);
              touch();
            }}
          >
            <svg viewBox="0 0 10 10" width="8" height="8" aria-hidden>
              <path
                d="M2 4l3 3 3-3"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>

        {view.phase === "working" && view.progress !== null ? (
          <div className="island-progress" aria-hidden>
            <span style={{ width: `${Math.round(view.progress * 100)}%` }} />
          </div>
        ) : null}

        <AnimatePresence initial={false}>
          {expanded ? (
            <motion.div
              className="island-card"
              initial={{ opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.14 }}
              onClick={touch}
            >
              {confirm ? (
                <div className="island-confirm">
                  <div className="confirm-head">
                    <span className="confirm-badge">
                      {confirm.tier?.toUpperCase() || "CONFIRM"}
                    </span>
                    <span className="confirm-tool">{confirm.tool}</span>
                    {expiresIn ? (
                      <span className="confirm-timer" title="Request expiry">
                        expires in {expiresIn}
                      </span>
                    ) : null}
                  </div>
                  <div className="confirm-args">
                    {confirm.message || argHint(confirm.args) || "Allow this action?"}
                  </div>
                  {argHint(confirm.args) && confirm.message ? (
                    <div className="confirm-args confirm-path">{argHint(confirm.args)}</div>
                  ) : null}
                  <div className="island-actions">
                    <button
                      className="btn btn-primary"
                      onClick={() => agent.respond(true)}
                    >
                      Allow
                    </button>
                    <button className="btn" onClick={() => agent.respond(false)}>
                      Deny
                    </button>
                    <span className="island-spacer" />
                    <button className="btn btn-quiet" onClick={openDashboard}>
                      Details
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="card-head">
                    <span className="card-eyebrow">{view.phase}</span>
                    <span className="island-spacer" />
                    <button className="card-link" onClick={openDashboard}>
                      Open dashboard ↗
                    </button>
                  </div>

                  {view.phase === "control" ? (
                    <div className="island-control-copy">
                      <div className="control-row">
                        <span className="control-label">target</span>
                        <span className="control-target">{view.controlTarget}</span>
                      </div>
                      <div className="control-hint">
                        Kill switch is always reachable: Stop above, or press
                        Ctrl+Alt+Esc.
                      </div>
                    </div>
                  ) : null}

                  {agent.voice.error ? (
                    <div className="island-answer island-error">
                      {agent.voice.error}
                    </div>
                  ) : null}

                  {view.phase === "listening" ? (
                    <div className="island-convo">
                      <div className="bubble bubble-you">
                        <span className="bubble-label">You</span>
                        <span className="bubble-text">
                          {agent.voice.partial || "Listening…"}
                        </span>
                      </div>
                    </div>
                  ) : null}

                  {view.phase === "speaking" && agent.voice.spoken ? (
                    <div className="island-convo">
                      <div className="bubble bubble-agent">
                        <span className="bubble-label">WinBrow</span>
                        <span className="bubble-text">{agent.voice.spoken}</span>
                      </div>
                    </div>
                  ) : null}

                  {view.phase === "working" ? (
                    <div className="island-live">
                      <span className="pulse" />
                      <span className="island-live-skill">
                        {skillVerb(view.liveSkill)}
                      </span>
                      <span className="island-live-hint">{view.detail}</span>
                    </div>
                  ) : null}

                  {view.phase !== "listening" &&
                  view.phase !== "speaking" &&
                  !view.answer &&
                  view.goal &&
                  view.phase !== "control" ? (
                    <div className="island-goal-full">{view.goal}</div>
                  ) : null}

                  {view.answer ? (
                    <div className="island-answer">{view.answer}</div>
                  ) : null}

                  {view.error ? (
                    <div className="island-answer island-error">{view.error}</div>
                  ) : null}

                  {!connected ? (
                    <div className="island-answer island-error">
                      {agent.connection === "offline"
                        ? "The Python sidecar is offline."
                        : "Connecting to the sidecar…"}
                      {sidecarError ? ` ${sidecarError}` : ""}
                    </div>
                  ) : null}

                  <form
                    className="island-composer"
                    onSubmit={(event) => {
                      event.preventDefault();
                      send();
                    }}
                  >
                    <input
                      ref={inputRef}
                      value={draft}
                      placeholder={
                        view.phase === "working"
                          ? "Queue the next task…"
                          : "Type instead of speaking…"
                      }
                      onChange={(event) => setDraft(event.target.value)}
                      onFocus={() => {
                        if (blurTimer.current) {
                          window.clearTimeout(blurTimer.current);
                          blurTimer.current = null;
                        }
                        api?.focusIsland();
                        touch();
                      }}
                      onBlur={() => {
                        blurTimer.current = window.setTimeout(() => {
                          blurTimer.current = null;
                          api?.blurIsland();
                        }, 120);
                      }}
                    />
                    <button
                      type="submit"
                      className="btn btn-primary"
                      disabled={!draft.trim()}
                    >
                      Run
                    </button>
                  </form>

                  <div className="island-quick">
                    <button
                      className="chip"
                      onClick={() =>
                        quick("Take a screenshot and tell me what is on my screen.")
                      }
                    >
                      Screenshot &amp; ask
                    </button>
                    <button
                      className="chip"
                      onClick={() =>
                        quick("Summarize what is currently in my clipboard.")
                      }
                    >
                      Summarize clipboard
                    </button>
                    <button
                      className="chip"
                      onClick={() =>
                        quick("Summarize the window I am currently looking at.")
                      }
                    >
                      Summarize window
                    </button>
                  </div>

                  <div className="island-footer">
                    <button
                      className="chip"
                      onClick={toggleMic}
                      title={micTitle}
                    >
                      <span className={`mic-dot mic-${view.mic}`} />
                      {view.mic === "muted"
                        ? "muted"
                        : view.mic === "listening"
                          ? "listening"
                          : view.mic === "on"
                            ? "mic on"
                            : "mic off"}
                    </button>
                    <button
                      className="chip"
                      onClick={ptt}
                      title="Push to talk (Ctrl+Alt+T)"
                      disabled={agent.voice.muted}
                    >
                      Push to talk
                    </button>
                    {view.canStopSpeaking ? (
                      <button
                        className="chip chip-bad"
                        onClick={() => void agent.voiceAction("interrupt")}
                        title="Stop speaking (Ctrl+Alt+X)"
                      >
                        Stop speaking
                      </button>
                    ) : null}
                    <span className="island-spacer" />
                    <button
                      className="chip"
                      onClick={() =>
                        void agent.voiceAction(
                          agent.voice.running ? "stop" : "start-wake",
                        )
                      }
                      title="Start or stop the local voice loop"
                    >
                      {agent.voice.running ? "Voice off" : "Voice on"}
                    </button>
                    {!connected ? (
                      <button
                        className="chip"
                        onClick={() => {
                          api?.startSidecar();
                          setSidecarError("");
                        }}
                      >
                        Start sidecar
                      </button>
                    ) : null}
                  </div>

                  {agent.context?.active_app ? (
                    <button
                      className="context-chip"
                      onClick={openDashboard}
                      title="What the agent can currently see"
                    >
                      <span className="context-eye" aria-hidden>
                        ◉
                      </span>
                      <span className="context-app">{agent.context.active_app}</span>
                      {agent.context.active_title ? (
                        <span className="context-title">
                          {agent.context.active_title}
                        </span>
                      ) : null}
                    </button>
                  ) : null}

                  {view.phase === "done" || view.error ? (
                    <div className="island-actions">
                      {view.retryGoal ? (
                        <button
                          className="btn"
                          onClick={() => quick(view.retryGoal)}
                        >
                          Retry
                        </button>
                      ) : null}
                      <button className="btn btn-primary" onClick={openDashboard}>
                        Open
                      </button>
                    </div>
                  ) : null}
                </>
              )}
            </motion.div>
          ) : null}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}
