import type { TraceStep, VoiceState } from "../types";
import type { AgentState } from "./useAgent";
import { argHint, clock, snippet } from "./format";

export type IslandPhase =
  | "idle"
  | "listening"
  | "speaking"
  | "thinking"
  | "working"
  | "done"
  | "error"
  | "confirm"
  | "control";

export type MicState = "off" | "on" | "listening" | "muted";

export type IslandSize = "small" | "medium";

export interface IslandView {
  phase: IslandPhase;
  size: IslandSize;
  status: string;
  detail?: string;
  mic: MicState;
  badge: number;
  stepTotal: number;
  stepIndex: number;
  elapsedLabel?: string;
  canCancel: boolean;
  progress: number | null;
  liveSkill?: string;
  answer?: string;
  error?: string;
  goal?: string;
  retryGoal: string;
  mustStayOpen: boolean;
  controlTarget?: string;
  canStopSpeaking: boolean;
}

const DONE_HOLD_MS = 6000;

// Skills that take over the mouse/keyboard. While one is running the island
// switches to the high-contrast control banner with an always-visible stop.
const CONTROL_SKILLS = [
  "computer_click",
  "computer_type",
  "computer_key",
  "computer_scroll",
  "computer_paste",
  "type_text",
  "focus_window",
  "minimize_all_except",
  "file_dialog_save",
];

function isControlSkill(skill: string): boolean {
  const key = String(skill || "").toLowerCase();
  return CONTROL_SKILLS.some((prefix) => key.startsWith(prefix));
}

function micState(voice: VoiceState): MicState {
  if (!voice.running) return "off";
  if (voice.muted) return "muted";
  if (voice.phase === "listening") return "listening";
  return "on";
}

function stringArg(step: TraceStep | undefined): string {
  if (!step) return "";
  const args = step.args || {};
  const candidates = [
    args.app,
    args.title,
    args.name,
    args.window,
    args.target,
    args.url,
    args.text,
  ];
  for (const value of candidates) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return argHint(args);
}

function lastSummary(agent: AgentState, taskId?: string): string {
  const task = taskId
    ? agent.tasks[taskId]
    : Object.values(agent.tasks).sort((a, b) => b.updatedAt - a.updatedAt)[0];
  if (!task) return "";
  const summaries = task.thinking.filter((block) => block.kind === "summary");
  return snippet(summaries[summaries.length - 1]?.text || "", 120);
}

export function deriveIsland(
  agent: AgentState,
  now: number,
  expanded: boolean,
  flash: string,
): IslandView {
  const allTasks = Object.values(agent.tasks);
  const task = agent.active;
  const runningTasks = allTasks.filter(
    (candidate) => candidate.status === "running" && candidate.live,
  );
  const mic = micState(agent.voice);
  const badge = Math.max(
    0,
    runningTasks.filter((candidate) => candidate.id !== task?.id).length,
  );

  const base: IslandView = {
    phase: "idle",
    size: "small",
    status: "Ready",
    mic,
    badge,
    stepTotal: 0,
    stepIndex: 0,
    canCancel: false,
    progress: null,
    mustStayOpen: false,
    canStopSpeaking: false,
    retryGoal: "",
  };

  // A local flash ("Stopped", "Request expired", sidecar errors) outranks the
  // task state for its short lifetime.
  if (flash) {
    const bad = /expired|offline|error|failed|unavailable|could not/i.test(flash);
    return {
      ...base,
      phase: bad ? "error" : "done",
      size: "small",
      status: flash,
      answer: flash,
      error: bad ? flash : undefined,
    };
  }

  // 1. Permission prompts block the run until the user decides.
  if (agent.confirm) {
    const hint = argHint(agent.confirm.args);
    return {
      ...base,
      phase: "confirm",
      size: "medium",
      status: "Confirm needed",
      detail: [agent.confirm.tool, hint].filter(Boolean).join(" · "),
      canCancel: true,
      mustStayOpen: true,
      goal: task?.goal,
    };
  }

  if (task) {
    const runningStep =
      task.steps.find((step) => step.status === "running") ||
      (task.live ? task.steps[task.steps.length - 1] : undefined);

    // 2. Control mode: the agent is driving the mouse/keyboard.
    if (runningStep && isControlSkill(runningStep.skill)) {
      return {
        ...base,
        phase: "control",
        size: "medium",
        status: "Controlling computer",
        detail: stringArg(runningStep),
        controlTarget: stringArg(runningStep) || runningStep.skill,
        liveSkill: runningStep.skill,
        elapsedLabel: clock(now - (task.startedAt || task.createdAt)),
        canCancel: true,
        mustStayOpen: true,
        goal: task.goal,
      };
    }

    // 3. Failures persist until another task starts.
    if (task.status === "failed") {
      const message = task.error || task.answer || "Task failed.";
      return {
        ...base,
        phase: "error",
        size: "medium",
        status: "Failed",
        detail: snippet(message, 120),
        error: message,
        retryGoal: task.goal,
        goal: task.goal,
        canCancel: false,
      };
    }
  }

  // 4. Voice.
  if (
    agent.voice.running &&
    !agent.voice.muted &&
    agent.voice.phase === "listening"
  ) {
    return {
      ...base,
      phase: "listening",
      size: "medium",
      status: "Listening",
      detail: agent.voice.partial || "Speak now",
    };
  }

  if (agent.voice.phase === "speaking" && agent.voice.spoken) {
    return {
      ...base,
      phase: "speaking",
      size: "medium",
      status: "Speaking",
      detail: snippet(agent.voice.spoken, 120),
      canStopSpeaking: true,
    };
  }

  // 5. A clarifying question also blocks the run, but there is no permission
  //    prompt: show the question with the normal card and a question icon.
  if (task?.needsInput) {
    const question = task.question || task.answer || task.goal;
    return {
      ...base,
      phase: "confirm",
      size: "medium",
      status: "Needs input",
      detail: snippet(question, 120),
      answer: question,
      goal: task.goal,
      canCancel: true,
      mustStayOpen: true,
    };
  }

  // 6. Task progress.
  if (task && (task.live || task.status === "running" || task.status === "queued")) {
    const runningStep = task.steps.find((step) => step.status === "running");
    const index = runningStep ? task.steps.indexOf(runningStep) : -1;
    const stepIndex = index >= 0 ? index + 1 : 0;
    const stepTotal = task.steps.length;
    const thinking = lastSummary(agent, task.id);
    const detail = runningStep
      ? argHint(runningStep.args) || runningStep.why || task.goal
      : thinking || task.goal;

    return {
      ...base,
      phase: runningStep ? "working" : "thinking",
      size: "medium",
      status: runningStep ? "Working" : "Thinking",
      detail: snippet(detail, 140),
      liveSkill: runningStep?.skill,
      stepIndex,
      stepTotal,
      progress: stepTotal > 0 ? Math.min(1, stepIndex / stepTotal) : null,
      elapsedLabel: clock(now - (task.startedAt || task.createdAt)),
      canCancel: true,
      answer: task.answer || undefined,
      goal: task.goal,
    };
  }

  // 7. Transient success / cancellation notice.
  const doneRecent =
    task &&
    (task.status === "completed" || task.status === "cancelled") &&
    now - task.updatedAt < DONE_HOLD_MS;
  const pinnedDone = agent.demo === "done" && task;
  if ((doneRecent || pinnedDone) && task) {
    const stopped = task.status === "cancelled";
    return {
      ...base,
      phase: "done",
      size: "small",
      status: stopped ? "Stopped" : "Done",
      answer: snippet(task.answer || task.summary || "", 160),
      goal: task.goal,
    };
  }

  // Idle. The connection state is shown in the pill so an offline sidecar is
  // obvious without opening the card.
  return {
    ...base,
    phase: "idle",
    size: "small",
    status:
      agent.connection === "offline"
        ? "Offline"
        : agent.connection === "connecting"
          ? "Connecting…"
          : "Ready",
    detail:
      agent.connection === "offline" ? "Sidecar unavailable" : undefined,
  };
}
