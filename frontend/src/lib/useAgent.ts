import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  SERVER_WS,
  apiDelete,
  apiGet,
  apiPost,
} from "./agent";
import type {
  ActivityEntry,
  ChatSession,
  ConfirmRequest,
  ConnectionLogEntry,
  ConnectionLogKind,
  ConnectionLogLevel,
  ConnectionLogSource,
  ConnectionState,
  ContextSnapshot,
  SidecarTask,
  TaskView,
  ThinkingBlock,
  TraceStep,
  VoicePhase,
  VoiceState,
} from "../types";

type Json = Record<string, any>;

export interface AgentState {
  connection: ConnectionState;
  tasks: Record<string, TaskView>;
  remote: SidecarTask[];
  sessions: Record<string, ChatSession>;
  sessionOrder: string[];
  activeSessionId: string;
  activeSession?: ChatSession;
  activeTasks: TaskView[];
  active?: TaskView;
  confirm: ConfirmRequest | null;
  context?: ContextSnapshot;
  voice: VoiceState;
  activity: ActivityEntry[];
  connectionLog: ConnectionLogEntry[];
  demo?: string;
  run: (goal: string, parentId?: string) => void;
  cancel: () => void;
  newChat: () => void;
  selectSession: (id: string) => void;
  deleteChat: (id: string) => Promise<void>;
  refresh: () => Promise<void>;
  select: (taskId: string) => void;
  respond: (approve: boolean) => Promise<void>;
  refreshContext: () => void;
  dismissConfirm: () => void;
  voiceAction: (action: string) => Promise<void>;
  clearActivity: () => void;
  clearConnectionLog: () => void;
}

const EMPTY_VOICE: VoiceState = {
  running: false,
  mode: "",
  muted: false,
  error: "",
  phase: "off",
  level: 0,
  partial: "",
  transcript: "",
  spoken: "",
  busy: false,
};

const PARENTS_KEY = "winbrow.parents";
const VOICE_PHASES: VoicePhase[] = [
  "off",
  "idle",
  "listening",
  "thinking",
  "speaking",
];

function newSessionId(): string {
  return `chat_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

function toMs(value: unknown): number {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return 0;
  return number > 1e12 ? number : number * 1000;
}

function normalizePhase(value: unknown): VoicePhase {
  const phase = String(value || "").toLowerCase();
  return VOICE_PHASES.includes(phase as VoicePhase)
    ? (phase as VoicePhase)
    : "idle";
}

function normalizeVoice(raw: unknown): VoiceState {
  const data = (raw && typeof raw === "object" ? raw : {}) as Json;
  const running = Boolean(data.running);
  return {
    running,
    mode: String(data.mode || ""),
    muted: Boolean(data.muted),
    error: String(data.error || ""),
    phase: running ? normalizePhase(data.state ?? data.phase) : "off",
    level: Math.max(0, Math.min(1, Number(data.level) || 0)),
    partial: String(data.partial || ""),
    transcript: String(data.last_text ?? data.transcript ?? ""),
    spoken: String(data.spoken || ""),
    busy: Boolean(data.busy),
  };
}

function readParentMap(): Record<string, string> {
  try {
    const raw = localStorage.getItem(PARENTS_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function readParent(taskId: string): string {
  return readParentMap()[taskId] || "";
}

function writeParent(taskId: string, parentId: string): void {
  if (!taskId || !parentId) return;
  try {
    const map = readParentMap();
    map[taskId] = parentId;
    const keys = Object.keys(map);
    if (keys.length > 300) {
      for (const key of keys.slice(0, keys.length - 300)) delete map[key];
    }
    localStorage.setItem(PARENTS_KEY, JSON.stringify(map));
  } catch {
    // localStorage can be unavailable in hardened renderers.
  }
}

function normalizeStatus(value: unknown): TaskView["status"] {
  const status = String(value || "").toLowerCase();
  if (status === "queued") return "queued";
  if (status === "running") return "running";
  if (status === "failed") return "failed";
  if (status === "cancelled" || status === "canceled") return "cancelled";
  return "completed";
}

function sessionFromApi(raw: Json): ChatSession {
  return {
    id: String(raw.session_id || raw.id || ""),
    title: String(raw.title || "(untitled chat)"),
    createdAt: toMs(raw.created_at),
    updatedAt: toMs(raw.updated_at),
    status: String(raw.status || "completed"),
    taskIds: raw.latest_task_id ? [String(raw.latest_task_id)] : [],
    taskCount: Number(raw.task_count) || 0,
  };
}

function stepFromReplay(raw: Json, taskId: string, index: number): TraceStep {
  const status = String(raw.status || "");
  const stepStatus: TraceStep["status"] =
    status === "running"
      ? "running"
      : status === "success"
        ? "success"
        : status === "skipped"
          ? "skipped"
          : status === "cancelled"
            ? "cancelled"
            : raw.success
              ? "success"
              : "failed";
  return {
    id: String(raw.id || `${taskId}-step-${index}`),
    skill: String(raw.skill || "step"),
    args:
      raw.args && typeof raw.args === "object"
        ? (raw.args as Record<string, unknown>)
        : {},
    why: raw.why ? String(raw.why) : undefined,
    status: stepStatus,
    output: raw.output ? String(raw.output) : undefined,
    warning: raw.warning ? String(raw.warning) : undefined,
    elapsedMs: Number(raw.elapsed_ms) || undefined,
    startedAt: toMs(raw.started_at) || Date.now(),
    endedAt: toMs(raw.ended_at) || undefined,
  };
}

/** Fold the persisted event log back into the same TaskView shape used live. */
function taskFromReplay(raw: Json): TaskView {
  const id = String(raw.id || "");
  const createdAt = toMs(raw.created_at) || Date.now();
  const updatedAt = toMs(raw.updated_at) || createdAt;
  const task: TaskView = {
    id,
    sessionId: String(raw.session_id || ""),
    goal: String(raw.goal || ""),
    status: normalizeStatus(raw.status),
    createdAt,
    updatedAt,
    thinking: [],
    steps: [],
    answer: "",
    summary: String(raw.summary || ""),
    error: String(raw.error || ""),
    live: String(raw.status || "") === "running",
    startedAt: createdAt,
    parentId: readParent(id) || undefined,
  };

  let lastRaw: ThinkingBlock | null = null;
  const steps = new Map<string, TraceStep>();

  const events = Array.isArray(raw.events) ? (raw.events as Json[]) : [];
  for (const event of events) {
    const payload = (event.payload || {}) as Json;
    const type = String(event.type || "");
    const ts = toMs(event.ts) || updatedAt;
    const stepId = String(event.step_id || "");
    switch (type) {
      case "task.started":
        task.goal = String(payload.goal || task.goal);
        task.sessionId = String(payload.session_id || task.sessionId);
        task.startedAt = ts;
        break;
      case "thinking.summary":
        lastRaw = null;
        task.thinking.push({
          id: `${id}-summary-${event.seq ?? task.thinking.length}`,
          kind: "summary",
          text: String(payload.summary || payload.text || ""),
          ts: ts / 1000,
        });
        break;
      case "thinking.delta": {
        const text = String(payload.text || "");
        if (!text) break;
        if (lastRaw) {
          lastRaw.text += text;
          lastRaw.ts = ts / 1000;
        } else {
          lastRaw = {
            id: `${id}-raw-${event.seq ?? task.thinking.length}`,
            kind: "raw",
            text,
            ts: ts / 1000,
          };
          task.thinking.push(lastRaw);
        }
        break;
      }
      case "tool.started": {
        const step: TraceStep = {
          id: stepId || `${id}-step-${event.seq ?? steps.size}`,
          skill: String(payload.skill || "step"),
          args:
            payload.args && typeof payload.args === "object"
              ? (payload.args as Record<string, unknown>)
              : {},
          why: payload.why ? String(payload.why) : undefined,
          status: "running",
          startedAt: ts,
        };
        steps.set(step.id, step);
        break;
      }
      case "tool.result": {
        const key = stepId || `${id}-step-${event.seq ?? steps.size}`;
        const existing =
          steps.get(key) ||
          ({
            id: key,
            skill: String(payload.skill || "step"),
            args: {},
            status: "running",
            startedAt: ts,
          } as TraceStep);
        existing.status = payload.success ? "success" : "failed";
        existing.output = payload.output ? String(payload.output) : existing.output;
        existing.warning = payload.warning
          ? String(payload.warning)
          : existing.warning;
        existing.elapsedMs =
          Number(payload.elapsed_ms) || existing.elapsedMs;
        existing.endedAt = ts;
        steps.set(key, existing);
        break;
      }
      case "answer.delta":
        task.answer += String(payload.text || "");
        break;
      case "answer.final":
        task.answer = String(payload.text || "");
        break;
      case "task.completed": {
        const needsInput = Boolean(payload.needs_input);
        const success = Boolean(payload.success);
        task.status = needsInput || success ? "completed" : "failed";
        task.live = false;
        task.summary = String(payload.summary || task.summary || "");
        task.needsInput = needsInput;
        task.question = needsInput
          ? String(payload.question || task.question || "")
          : undefined;
        task.options = Array.isArray(payload.options)
          ? payload.options.map(String)
          : undefined;
        task.evidence = (payload.evidence as Record<string, unknown>) || undefined;
        if (!success && !needsInput) {
          task.error = task.summary || "Task failed.";
        } else if (success && !task.answer && task.summary) {
          task.answer = task.summary;
        }
        break;
      }
      case "task.failed":
        task.status = "failed";
        task.live = false;
        task.error = String(payload.error || "Task failed.");
        break;
      case "task.cancelled":
        task.status = "cancelled";
        task.live = false;
        break;
      default:
        break;
    }
  }

  task.steps = [...steps.values()];

  // Older rows may have steps but no tool events (for example tasks created
  // before the v2 bus). Fall back to the persisted steps table.
  if (!task.steps.length && Array.isArray(raw.steps)) {
    task.steps = (raw.steps as Json[]).map((step, index) =>
      stepFromReplay(step, id, index),
    );
  }
  if (
    !task.answer &&
    !task.needsInput &&
    !task.error &&
    task.status === "completed" &&
    task.summary
  ) {
    task.answer = task.summary;
  }
  return task;
}

function stepFromExecution(
  raw: Json,
  task: TaskView,
  index: number,
): TraceStep {
  const id = String(raw.step_id || `${task.id}-confirm-${index}`);
  const existing = task.steps.find((step) => step.id === id);
  const success = raw.success !== false;
  return {
    id,
    skill: String(raw.skill || existing?.skill || "step"),
    args:
      raw.args && typeof raw.args === "object"
        ? (raw.args as Record<string, unknown>)
        : existing?.args || {},
    why: raw.why ? String(raw.why) : existing?.why,
    status: success ? "success" : "failed",
    output: raw.output !== undefined ? String(raw.output) : existing?.output,
    warning: raw.warning ? String(raw.warning) : existing?.warning,
    elapsedMs: Number(raw.elapsed_ms) || existing?.elapsedMs,
    startedAt: existing?.startedAt || Date.now(),
    endedAt: Date.now(),
  };
}

// --------------------------------------------------------------------------- demo
interface DemoState {
  tasks: Record<string, TaskView>;
  sessions: Record<string, ChatSession>;
  sessionOrder: string[];
  activeSessionId: string;
  confirm: ConfirmRequest | null;
  voice: VoiceState;
  context: ContextSnapshot;
}

function demoTask(overrides: Partial<TaskView> = {}): TaskView {
  const now = Date.now();
  return {
    id: "task_demo",
    sessionId: "demo",
    goal: "Find the latest invoice on my Desktop and summarize it",
    status: "running",
    createdAt: now - 15_000,
    updatedAt: now,
    startedAt: now - 15_000,
    live: true,
    thinking: [
      {
        id: "demo-think",
        kind: "summary",
        text:
          "I’ll scan the Desktop for invoice files, open the newest one, and summarize the totals.",
        ts: Math.floor((now - 14_000) / 1000),
      },
    ],
    steps: [
      {
        id: "demo-step-1",
        skill: "find_files",
        args: { path: "C:\\Users\\you\\Desktop", pattern: "*invoice*" },
        status: "success",
        output: "Found 3 matching files.",
        elapsedMs: 420,
        startedAt: now - 13_000,
        endedAt: now - 12_580,
      },
      {
        id: "demo-step-2",
        skill: "read_document",
        args: { path: "C:\\Users\\you\\Desktop\\Invoice-2041.pdf" },
        status: "success",
        output: "Invoice #2041 · total ₹48,250",
        elapsedMs: 860,
        startedAt: now - 12_000,
        endedAt: now - 11_140,
      },
      {
        id: "demo-step-3",
        skill: "browser_search",
        args: { query: "current weather radar Kochi" },
        why: "Check the weather before suggesting an outdoor task.",
        status: "running",
        startedAt: now - 4_000,
      },
    ],
    answer: "",
    ...overrides,
  };
}

function buildDemoState(demo: string): DemoState {
  const now = Date.now();
  const baseSession: ChatSession = {
    id: "demo",
    title: "Invoice follow-up",
    createdAt: now - 60_000,
    updatedAt: now,
    status: "running",
    taskIds: ["task_demo"],
    taskCount: 1,
  };
  const empty = {
    tasks: {} as Record<string, TaskView>,
    sessions: { demo: baseSession } as Record<string, ChatSession>,
    sessionOrder: ["demo"],
    activeSessionId: "demo",
    confirm: null as ConfirmRequest | null,
    voice: { ...EMPTY_VOICE },
    context: { active_app: "WordPad", active_title: "Document - WordPad" },
  };

  const pin = demo === "1" || !demo ? "working" : demo;
  if (pin === "idle") {
    return {
      ...empty,
      sessions: { demo: { ...baseSession, status: "completed" } },
      tasks: {},
    };
  }
  if (pin === "listening") {
    return {
      ...empty,
      voice: {
        ...EMPTY_VOICE,
        running: true,
        mode: "vad",
        phase: "listening",
        partial: "summarize the latest invoice",
        level: 0.42,
      },
      tasks: {
        task_demo: demoTask({
          status: "completed",
          live: false,
          updatedAt: now - 60_000,
          answer: "The latest invoice is Invoice-2041.pdf for ₹48,250.",
        }),
      },
    };
  }
  if (pin === "speaking") {
    return {
      ...empty,
      voice: {
        ...EMPTY_VOICE,
        running: true,
        mode: "vad",
        phase: "speaking",
        spoken:
          "I found three invoices. The newest is Invoice-2041.pdf, dated today, for forty-eight thousand two hundred and fifty rupees.",
      },
      tasks: {
        task_demo: demoTask({
          status: "completed",
          live: false,
          updatedAt: now - 60_000,
          answer: "The latest invoice is Invoice-2041.pdf for ₹48,250.",
        }),
      },
    };
  }
  if (pin === "confirm") {
    return {
      ...empty,
      confirm: {
        confirmationId: "demo-confirm",
        tier: "t2",
        tool: "write_file",
        args: { path: "C:\\Users\\you\\Desktop\\invoice-summary.md" },
        message: "Allow T2 action 'write_file'?",
        createdAt: now - 2_000,
        expiresAt: now + 58_000,
        taskId: "task_demo",
      },
      tasks: {
        task_demo: demoTask({
          status: "completed",
          live: false,
          updatedAt: now - 60_000,
          needsInput: true,
          question: "Allow writing the summary to the Desktop?",
        }),
      },
    };
  }
  if (pin === "control") {
    return {
      ...empty,
      tasks: {
        task_demo: demoTask({
          steps: [
            {
              id: "demo-step-1",
              skill: "computer_click",
              args: { x: 742, y: 418, app: "WordPad" },
              status: "running",
              startedAt: now - 3_000,
            },
          ],
        }),
      },
    };
  }
  if (pin === "done") {
    return {
      ...empty,
      sessions: { demo: { ...baseSession, status: "completed" } },
      tasks: {
        task_demo: demoTask({
          status: "completed",
          live: false,
          answer: [
            "## Invoice summary",
            "",
            "The latest invoice is **Invoice-2041.pdf**, dated today.",
            "",
            "| Item | Amount |",
            "|---|---|",
            "| Design retainer | ₹32,000 |",
            "| Hosting | ₹4,250 |",
            "| Support | ₹12,000 |",
            "| **Total** | **₹48,250** |",
            "",
            "The file is on your Desktop.",
          ].join("\n"),
        }),
      },
    };
  }
  if (pin === "error") {
    return {
      ...empty,
      sessions: { demo: { ...baseSession, status: "failed" } },
      tasks: {
        task_demo: demoTask({
          status: "failed",
          live: false,
          error:
            "I couldn't read Invoice-2041.pdf: the file is password-protected.",
        }),
      },
    };
  }
  // demo=1 / demo=working (and any unknown value) seed a live working task.
  return { ...empty, tasks: { task_demo: demoTask() } };
}

// --------------------------------------------------------------------------- hook
export function useAgent(): AgentState {
  const params = useMemo(
    () => new URLSearchParams(window.location.search),
    [],
  );
  const demoParam = params.get("demo");
  const isDemo = demoParam !== null;
  const demo = isDemo ? demoParam || "1" : "";
  const demoState = useMemo(
    () => (isDemo ? buildDemoState(demo) : null),
    [isDemo, demo],
  );

  const isDemoRef = useRef(isDemo);
  isDemoRef.current = isDemo;

  const [connection, setConnection] = useState<ConnectionState>(
    demoState ? "online" : "connecting",
  );
  const [tasks, setTasks] = useState<Record<string, TaskView>>(
    () => demoState?.tasks || {},
  );
  const [remote, setRemote] = useState<SidecarTask[]>([]);
  const [sessions, setSessions] = useState<Record<string, ChatSession>>(
    () => demoState?.sessions || {},
  );
  const [sessionOrder, setSessionOrder] = useState<string[]>(
    () => demoState?.sessionOrder || [],
  );
  const [activeSessionId, setActiveSessionId] = useState(
    () => demoState?.activeSessionId || "",
  );
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(
    () => demoState?.confirm || null,
  );
  const [context, setContext] = useState<ContextSnapshot | undefined>(
    () => demoState?.context,
  );
  const [voice, setVoice] = useState<VoiceState>(
    () => demoState?.voice || EMPTY_VOICE,
  );
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [connectionLog, setConnectionLog] = useState<ConnectionLogEntry[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  // The WebSocket is the authoritative liveness signal: the desktop shell's
  // health probe can time out while a task saturates the machine, but an open
  // socket still means the sidecar is reachable.
  const wsOpenRef = useRef(false);
  const lastHealthWarnRef = useRef(0);
  const seenRef = useRef<Set<string>>(new Set());
  const pendingRunsRef = useRef<
    Array<{ parentId: string; goal: string; sessionId: string }>
  >([]);
  const pendingParentRef = useRef<Record<string, string>>({});
  const tasksRef = useRef(tasks);
  const sessionsRef = useRef(sessions);
  const sessionOrderRef = useRef(sessionOrder);
  const activeSessionIdRef = useRef(activeSessionId);
  const confirmRef = useRef<ConfirmRequest | null>(confirm);
  const logSeqRef = useRef(0);
  // High-frequency streaming deltas are coalesced so React re-renders at most
  // ~20 times/second instead of once per 32-char chunk.
  const deltaBufferRef = useRef<
    Record<string, { thinking: string; answer: string }>
  >({});
  const deltaTimerRef = useRef<number | null>(null);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);
  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);
  useEffect(() => {
    sessionOrderRef.current = sessionOrder;
  }, [sessionOrder]);
  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);
  useEffect(() => {
    confirmRef.current = confirm;
  }, [confirm]);

  const commitTasks = useCallback(
    (updater: (prev: Record<string, TaskView>) => Record<string, TaskView>) => {
      setTasks((prev) => {
        const next = updater(prev);
        tasksRef.current = next;
        return next;
      });
    },
    [],
  );

  const commitSessions = useCallback(
    (updater: (prev: Record<string, ChatSession>) => Record<string, ChatSession>) => {
      setSessions((prev) => {
        const next = updater(prev);
        sessionsRef.current = next;
        return next;
      });
    },
    [],
  );

  const commitSessionOrder = useCallback((updater: (prev: string[]) => string[]) => {
    setSessionOrder((prev) => {
      const next = updater(prev);
      sessionOrderRef.current = next;
      return next;
    });
  }, []);

  const setActiveSession = useCallback((id: string) => {
    activeSessionIdRef.current = id;
    setActiveSessionId(id);
  }, []);

  const setConfirmState = useCallback((next: ConfirmRequest | null) => {
    confirmRef.current = next;
    setConfirm(next);
  }, []);

  const pushConnectionLog = useCallback((entry: ConnectionLogEntry) => {
    setConnectionLog((prev) => {
      if (prev.some((item) => item.id === entry.id)) return prev;
      const next = [...prev, entry];
      return next.length > 500 ? next.slice(-500) : next;
    });
  }, []);

  const log = useCallback(
    (
      level: ConnectionLogLevel,
      message: string,
      detail = "",
      kind: ConnectionLogKind = "connection",
      source: ConnectionLogSource = "host",
    ) => {
      pushConnectionLog({
        id: `ui-${Date.now()}-${++logSeqRef.current}`,
        ts: Date.now(),
        level,
        source,
        kind,
        message: String(message || "").slice(0, 200),
        detail: String(detail || "").slice(0, 800),
      });
    },
    [pushConnectionLog],
  );

  const addActivity = useCallback((entry: ActivityEntry) => {
    setActivity((prev) => {
      const index = prev.findIndex((item) => item.id === entry.id);
      let next: ActivityEntry[];
      if (index >= 0) {
        next = prev.slice();
        next[index] = entry;
      } else {
        next = [...prev, entry];
      }
      return next.length > 800 ? next.slice(-800) : next;
    });
  }, []);

  const appendRawActivity = useCallback(
    (taskId: string, ts: number, text: string) => {
      setActivity((prev) => {
        const id = `raw-${taskId}`;
        const index = prev.findIndex((item) => item.id === id);
        if (index < 0) {
          const entry: ActivityEntry = {
            id,
            ts,
            kind: "thinking",
            status: "running",
            taskId,
            title: "Reasoning",
            body: text,
            detail: "raw provider reasoning",
          };
          return [...prev, entry].slice(-800);
        }
        const existing = prev[index];
        const next = prev.slice();
        next[index] = {
          ...existing,
          ts,
          body: `${existing.body || ""}${text}`,
          status: "running",
        };
        return next;
      });
    },
    [],
  );

  const flushDeltas = useCallback(() => {
    if (deltaTimerRef.current !== null) {
      window.clearTimeout(deltaTimerRef.current);
      deltaTimerRef.current = null;
    }
    const entries = Object.entries(deltaBufferRef.current);
    if (!entries.length) return;
    deltaBufferRef.current = {};
    const now = Date.now();

    commitTasks((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [taskId, buffer] of entries) {
        const existing = next[taskId];
        if (!existing) continue;
        let thinking = existing.thinking;
        if (buffer.thinking) {
          thinking = thinking.slice();
          const last = thinking[thinking.length - 1];
          if (last && last.kind === "raw") {
            thinking[thinking.length - 1] = {
              ...last,
              text: last.text + buffer.thinking,
              ts: now / 1000,
            };
          } else {
            thinking.push({
              id: `${taskId}-raw-${now}-${thinking.length}`,
              kind: "raw",
              text: buffer.thinking,
              ts: now / 1000,
            });
          }
        }
        next[taskId] = {
          ...existing,
          live: true,
          updatedAt: now,
          thinking,
          answer: buffer.answer
            ? `${existing.answer || ""}${buffer.answer}`
            : existing.answer,
        };
        changed = true;
      }
      return changed ? next : prev;
    });

    for (const [taskId, buffer] of entries) {
      if (buffer.thinking) appendRawActivity(taskId, now, buffer.thinking);
    }
  }, [appendRawActivity, commitTasks]);

  const ensureSessionForTask = useCallback(
    (sessionId: string, goal: string, taskId: string, ts: number) => {
      if (!sessionId) return;
      commitSessions((prev) => {
        const existing = prev[sessionId];
        if (existing) {
          const taskIds = !taskId
            ? existing.taskIds
            : existing.taskIds.includes(taskId)
              ? existing.taskIds
              : [...existing.taskIds, taskId];
          return {
            ...prev,
            [sessionId]: {
              ...existing,
              title: existing.title || goal || "New chat",
              status: "running",
              updatedAt: ts,
              taskIds,
            },
          };
        }
        return {
          ...prev,
          [sessionId]: {
            id: sessionId,
            title: goal || "New chat",
            createdAt: ts,
            updatedAt: ts,
            status: "running",
            taskIds: taskId ? [taskId] : [],
            taskCount: taskId ? 1 : 0,
          },
        };
      });
      commitSessionOrder((prev) => [
        sessionId,
        ...prev.filter((item) => item !== sessionId),
      ]);
    },
    [commitSessions, commitSessionOrder],
  );

  const markSession = useCallback(
    (sessionId: string, status: string, ts: number) => {
      if (!sessionId) return;
      commitSessions((prev) =>
        prev[sessionId]
          ? {
              ...prev,
              [sessionId]: {
                ...prev[sessionId],
                status,
                updatedAt: ts,
              },
            }
          : prev,
      );
      commitSessionOrder((prev) => [
        sessionId,
        ...prev.filter((item) => item !== sessionId),
      ]);
    },
    [commitSessions, commitSessionOrder],
  );

  const markSessionForTask = useCallback(
    (taskId: string, status: string) => {
      const task = tasksRef.current[taskId];
      if (task?.sessionId) markSession(task.sessionId, status, Date.now());
    },
    [markSession],
  );

  const finalizeFromExecution = useCallback(
    (taskId: string, result: Json | undefined | null) => {
      if (!taskId || !result) return;
      const execution = (result.execution || {}) as Json;
      const ok = Boolean(execution.success);
      const needsInput = Boolean(execution.needs_input);
      const output = String(execution.output || "");
      const rawSteps = Array.isArray(execution.steps)
        ? (execution.steps as Json[])
        : null;
      const awaitingConfirm = confirmRef.current?.taskId === taskId;
      const now = Date.now();

      commitTasks((prev) => {
        const existing = prev[taskId];
        if (!existing) return prev;
        return {
          ...prev,
          [taskId]: {
            ...existing,
            status: needsInput ? "completed" : ok ? "completed" : "failed",
            live: false,
            answer: needsInput ? existing.answer : output || existing.answer,
            error:
              !ok && !needsInput && !awaitingConfirm
                ? output || "Task failed."
                : existing.error,
            needsInput,
            question: needsInput
              ? String(execution.question || existing.question || "")
              : existing.question,
            options:
              needsInput && Array.isArray(execution.options)
                ? execution.options.map(String)
                : existing.options,
            evidence:
              (execution.evidence as Record<string, unknown>) ||
              existing.evidence,
            steps: rawSteps
              ? rawSteps.map((step, index) =>
                  stepFromExecution(step, existing, index),
                )
              : existing.steps,
            updatedAt: now,
          },
        };
      });
      markSessionForTask(
        taskId,
        needsInput ? "needs_input" : ok ? "completed" : "failed",
      );
    },
    [commitTasks, markSessionForTask],
  );

  const applyEnvelope = useCallback(
    (envelope: Json) => {
      const type = String(envelope.type || "");
      if (!type) return;
      const payload = (
        envelope.payload && typeof envelope.payload === "object"
          ? envelope.payload
          : {}
      ) as Json;
      const taskId = String(envelope.task_id || "");

      // Voice events are global (no task id).
      if (!taskId) {
        if (
          type === "state.changed" ||
          type === "transcript.partial" ||
          type === "transcript.final" ||
          type === "agent.speaking"
        ) {
          setVoice((prev) => {
            if (type === "state.changed") {
              const stopped = payload.voice === "stopped";
              const running = stopped ? false : prev.running || Boolean(payload.state);
              return {
                ...prev,
                running,
                phase: stopped
                  ? "off"
                  : normalizePhase(payload.state ?? prev.phase),
                muted:
                  payload.muted === undefined
                    ? prev.muted
                    : Boolean(payload.muted),
                level:
                  payload.level === undefined
                    ? prev.level
                    : Math.max(0, Math.min(1, Number(payload.level) || 0)),
                error: payload.error ? String(payload.error) : "",
              };
            }
            if (type === "transcript.partial") {
              return {
                ...prev,
                running: true,
                partial: String(payload.text || ""),
                phase: "listening",
              };
            }
            if (type === "transcript.final") {
              return {
                ...prev,
                running: true,
                transcript: String(payload.text || ""),
                partial: "",
                phase: "thinking",
              };
            }
            return {
              ...prev,
              running: true,
              spoken: String(payload.text || ""),
              phase: "speaking",
            };
          });
        }
        return;
      }

      const seq = Number(envelope.seq || 0);
      if (seq > 0) {
        const key = `${taskId}:${seq}`;
        if (seenRef.current.has(key)) return;
        seenRef.current.add(key);
        if (seenRef.current.size > 20_000) seenRef.current.clear();
      }

      const ts = toMs(envelope.ts) || Date.now();
      const stepId = String(envelope.step_id || "");

      const baseTask = (existing: TaskView | undefined): TaskView =>
        existing || {
          id: taskId,
          sessionId: String(payload.session_id || ""),
          goal: String(payload.goal || ""),
          status: "running",
          createdAt: ts,
          updatedAt: ts,
          thinking: [],
          steps: [],
          answer: "",
          live: true,
          startedAt: ts,
          parentId:
            pendingParentRef.current[taskId] || readParent(taskId) || undefined,
        };

      // Stream text is buffered and applied in batches; everything else is
      // ordered after the text that preceded it.
      if (type === "thinking.delta" || type === "answer.delta") {
        const text = String(payload.text || "");
        if (!text) return;
        const buffer =
          deltaBufferRef.current[taskId] || { thinking: "", answer: "" };
        if (type === "thinking.delta") buffer.thinking += text;
        else buffer.answer += text;
        deltaBufferRef.current[taskId] = buffer;
        if (deltaTimerRef.current === null) {
          deltaTimerRef.current = window.setTimeout(() => {
            deltaTimerRef.current = null;
            flushDeltas();
          }, 50);
        }
        return;
      }

      flushDeltas();

      switch (type) {
        case "task.started": {
          const parentId =
            pendingParentRef.current[taskId] || readParent(taskId) || "";
          if (parentId) writeParent(taskId, parentId);
          const sessionId = String(payload.session_id || "");
          const goal = String(payload.goal || "");
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            return {
              ...prev,
              [taskId]: {
                ...existing,
                sessionId: sessionId || existing.sessionId,
                goal: goal || existing.goal,
                status: "running",
                live: true,
                createdAt: ts,
                updatedAt: ts,
                startedAt: ts,
                parentId: parentId || existing.parentId,
                answer: "",
                error: "",
                needsInput: false,
              },
            };
          });
          ensureSessionForTask(sessionId, goal, taskId, ts);
          addActivity({
            id: `task-${taskId}-start`,
            ts,
            kind: "task",
            status: "running",
            taskId,
            title: goal || "Task started",
            detail: "started",
          });
          break;
        }

        case "thinking.summary": {
          const text = String(payload.summary || payload.text || "");
          if (!text) break;
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            const block: ThinkingBlock = {
              id: `${taskId}-summary-${seq || existing.thinking.length}`,
              kind: "summary",
              text,
              ts: ts / 1000,
            };
            return {
              ...prev,
              [taskId]: {
                ...existing,
                live: true,
                updatedAt: ts,
                thinking: [...existing.thinking, block],
              },
            };
          });
          addActivity({
            id: `plan-${taskId}-${seq}`,
            ts,
            kind: "thinking",
            status: "info",
            taskId,
            title: "Brain plan",
            detail: text,
            body: text,
          });
          break;
        }

        case "tool.started": {
          const id = stepId || `${taskId}-step-${seq}`;
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            if (existing.steps.some((step) => step.id === id)) {
              return prev;
            }
            const step: TraceStep = {
              id,
              skill: String(payload.skill || "step"),
              args:
                payload.args && typeof payload.args === "object"
                  ? (payload.args as Record<string, unknown>)
                  : {},
              why: payload.why ? String(payload.why) : undefined,
              status: "running",
              startedAt: ts,
            };
            return {
              ...prev,
              [taskId]: {
                ...existing,
                live: true,
                updatedAt: ts,
                steps: [...existing.steps, step],
              },
            };
          });
          addActivity({
            id: `step-${id}`,
            ts,
            kind: "step",
            status: "running",
            taskId,
            title: String(payload.skill || "step"),
            detail: payload.why ? String(payload.why) : undefined,
            body: payload.args
              ? JSON.stringify(payload.args, null, 2).slice(0, 4000)
              : undefined,
          });
          break;
        }

        case "tool.result": {
          const id = stepId || `${taskId}-step-${seq}`;
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            const index = existing.steps.findIndex((step) => step.id === id);
            const steps = existing.steps.slice();
            const step: TraceStep =
              index >= 0
                ? { ...steps[index] }
                : {
                    id,
                    skill: String(payload.skill || "step"),
                    args: {},
                    status: "running",
                    startedAt: ts,
                  };
            step.status = payload.success ? "success" : "failed";
            step.output = payload.output
              ? String(payload.output)
              : step.output;
            step.warning = payload.warning
              ? String(payload.warning)
              : step.warning;
            step.elapsedMs =
              Number(payload.elapsed_ms) || step.elapsedMs;
            step.endedAt = ts;
            if (index >= 0) steps[index] = step;
            else steps.push(step);
            return {
              ...prev,
              [taskId]: { ...existing, updatedAt: ts, steps },
            };
          });
          addActivity({
            id: `step-${id}`,
            ts,
            kind: "step",
            status: payload.success ? "success" : "failed",
            taskId,
            title: String(payload.skill || "step"),
            detail: payload.success ? "completed" : "failed",
            body: [
              payload.output ? String(payload.output) : "",
              payload.warning ? `⚠ ${payload.warning}` : "",
            ]
              .filter(Boolean)
              .join("\n\n")
              .slice(0, 4000),
            elapsedMs: Number(payload.elapsed_ms) || undefined,
          });
          break;
        }

        case "answer.final": {
          const text = String(payload.text || "");
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            return {
              ...prev,
              [taskId]: {
                ...existing,
                updatedAt: ts,
                answer: text || existing.answer,
              },
            };
          });
          addActivity({
            id: `answer-${taskId}-${seq}`,
            ts,
            kind: "answer",
            status: "success",
            taskId,
            title: "Answer",
            body: text.slice(0, 8000),
          });
          break;
        }

        case "task.completed": {
          const needsInput = Boolean(payload.needs_input);
          const success = Boolean(payload.success);
          const awaitingConfirm = confirmRef.current?.taskId === taskId;
          const summary = String(payload.summary || "");
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            return {
              ...prev,
              [taskId]: {
                ...existing,
                status: needsInput || success ? "completed" : "failed",
                live: false,
                updatedAt: ts,
                summary,
                needsInput,
                question: needsInput
                  ? String(payload.question || "")
                  : undefined,
                options: Array.isArray(payload.options)
                  ? payload.options.map(String)
                  : undefined,
                evidence:
                  (payload.evidence as Record<string, unknown>) || undefined,
                answer:
                  success && !needsInput && !existing.answer && summary
                    ? summary
                    : existing.answer,
                error:
                  !success && !needsInput && !awaitingConfirm
                    ? summary || "Task failed."
                    : existing.error,
              },
            };
          });
          markSessionForTask(
            taskId,
            needsInput
              ? "needs_input"
              : awaitingConfirm
                ? "needs_input"
                : success
                  ? "completed"
                  : "failed",
          );
          addActivity({
            id: `task-${taskId}-done`,
            ts,
            kind: "task",
            status: success || needsInput ? "success" : "failed",
            taskId,
            title: needsInput
              ? "Waiting for your answer"
              : success
                ? "Task completed"
                : "Task failed",
            detail: summary ? summary.slice(0, 160) : undefined,
            body: summary.slice(0, 8000),
          });
          break;
        }

        case "task.failed": {
          const message = String(payload.error || "Task failed.");
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            return {
              ...prev,
              [taskId]: {
                ...existing,
                status: "failed",
                live: false,
                updatedAt: ts,
                error: message,
              },
            };
          });
          markSessionForTask(taskId, "failed");
          addActivity({
            id: `task-${taskId}-done`,
            ts,
            kind: "task",
            status: "failed",
            taskId,
            title: "Task failed",
            body: message,
          });
          break;
        }

        case "task.cancelled": {
          commitTasks((prev) => {
            const existing = baseTask(prev[taskId]);
            return {
              ...prev,
              [taskId]: {
                ...existing,
                status: "cancelled",
                live: false,
                updatedAt: ts,
              },
            };
          });
          markSessionForTask(taskId, "cancelled");
          addActivity({
            id: `task-${taskId}-done`,
            ts,
            kind: "task",
            status: "info",
            taskId,
            title: "Task cancelled",
          });
          break;
        }

        case "confirm.required": {
          const created = toMs(payload.created);
          const expires = toMs(payload.expires_at);
          setConfirmState({
            confirmationId: String(payload.confirmation_id || ""),
            tier: String(payload.tier || ""),
            tool: String(payload.tool || ""),
            args:
              payload.args && typeof payload.args === "object"
                ? (payload.args as Record<string, unknown>)
                : {},
            utterance: payload.utterance
              ? String(payload.utterance)
              : undefined,
            message: payload.message ? String(payload.message) : undefined,
            createdAt: created || Date.now(),
            expiresAt: expires || undefined,
            taskId,
          });
          addActivity({
            id: `confirm-${taskId}-${seq}`,
            ts,
            kind: "confirm",
            status: "running",
            taskId,
            title: `Approval required · ${payload.tool || "action"}`,
            detail: payload.message ? String(payload.message) : undefined,
            body: payload.args
              ? JSON.stringify(payload.args, null, 2).slice(0, 4000)
              : undefined,
          });
          break;
        }

        default:
          break;
      }
    },
    [
      addActivity,
      commitTasks,
      ensureSessionForTask,
      flushDeltas,
      markSessionForTask,
      setConfirmState,
    ],
  );

  const handleMessage = useCallback(
    (raw: string) => {
      let message: Json;
      try {
        message = JSON.parse(raw) as Json;
      } catch {
        return;
      }
      if (!message || typeof message !== "object") return;
      const type = String(message.type || "");

      if (type === "routing") {
        const taskId = String(message.task_id || "");
        const pending = pendingRunsRef.current.shift();
        if (taskId && pending?.parentId) {
          pendingParentRef.current[taskId] = pending.parentId;
          writeParent(taskId, pending.parentId);
          commitTasks((prev) =>
            prev[taskId]
              ? {
                  ...prev,
                  [taskId]: { ...prev[taskId], parentId: pending.parentId },
                }
              : prev,
          );
        }
        return;
      }

      if (type === "confirm.result") {
        if (message.status === "denied" && message.confirmation_id) {
          if (
            confirmRef.current?.confirmationId ===
            String(message.confirmation_id)
          ) {
            setConfirmState(null);
          }
        }
        return;
      }

      if (type === "cancel.requested") {
        const taskId = String(message.task_id || "");
        if (taskId) {
          commitTasks((prev) =>
            prev[taskId]
              ? {
                  ...prev,
                  [taskId]: {
                    ...prev[taskId],
                    status: "cancelled",
                    live: false,
                    updatedAt: Date.now(),
                  },
                }
              : prev,
          );
          markSessionForTask(taskId, "cancelled");
        }
        if (message.confirmation_id) setConfirmState(null);
        return;
      }

      if (type === "error") {
        const taskId = String(message.task_id || "");
        const detail = String(message.error || "Task failed.");
        if (taskId) {
          commitTasks((prev) =>
            prev[taskId]
              ? {
                  ...prev,
                  [taskId]: {
                    ...prev[taskId],
                    status: "failed",
                    live: false,
                    error: detail,
                    updatedAt: Date.now(),
                  },
                }
              : prev,
          );
          markSessionForTask(taskId, "failed");
        }
        log("error", "Task error", detail);
        return;
      }

      if (type === "result") {
        finalizeFromExecution(String(message.task_id || ""), message.data);
        return;
      }

      // v2 envelopes (and voice events with no task id).
      applyEnvelope(message);
    },
    [
      applyEnvelope,
      commitTasks,
      finalizeFromExecution,
      log,
      markSessionForTask,
      setConfirmState,
    ],
  );

  const loadVoice = useCallback(async () => {
    if (isDemoRef.current) return;
    try {
      const data = await apiGet<Json>("/api/voice");
      setVoice(normalizeVoice(data));
    } catch {
      // Voice is optional; the WS stream keeps the state fresh when available.
    }
  }, []);

  const hydrateSession = useCallback(
    async (sessionId: string) => {
      if (!sessionId || isDemoRef.current) return;
      try {
        const data = await apiGet<{ tasks: SidecarTask[] }>(
          `/api/tasks?session_id=${encodeURIComponent(sessionId)}&limit=60`,
        );
        const rows = Array.isArray(data.tasks) ? data.tasks : [];
        const recent = rows.slice(-24);
        const details = await Promise.all(
          recent.map((row) =>
            apiGet<Json>(`/api/tasks/${encodeURIComponent(row.id)}`).catch(
              () => null,
            ),
          ),
        );
        const views = details
          .filter((item): item is Json => Boolean(item))
          .map(taskFromReplay);
        if (views.length) {
          commitTasks((prev) => {
            const next = { ...prev };
            for (const view of views) {
              const existing = next[view.id];
              if (existing?.live) continue;
              if (!existing || existing.updatedAt <= view.updatedAt) {
                next[view.id] = view;
              }
            }
            return next;
          });
        }
        commitSessions((prev) =>
          prev[sessionId]
            ? {
                ...prev,
                [sessionId]: {
                  ...prev[sessionId],
                  taskIds: rows.map((row) => row.id),
                },
              }
            : prev,
        );
      } catch (error) {
        log("warn", "Could not load chat history", String(error));
      }
    },
    [commitSessions, commitTasks, log],
  );

  const refreshContext = useCallback(async () => {
    if (isDemoRef.current) return;
    try {
      const next = await apiGet<ContextSnapshot>("/api/context");
      setContext(next);
    } catch {
      // Context is best-effort; the sidecar may still be starting.
    }
  }, []);

  const refresh = useCallback(async () => {
    if (isDemoRef.current) return;
    const [sessionsResult, tasksResult, voiceResult, contextResult, permsResult] =
      await Promise.allSettled([
        apiGet<{ sessions: Json[] }>("/api/sessions?limit=50"),
        apiGet<{ tasks: SidecarTask[] }>("/api/tasks?limit=100"),
        apiGet<Json>("/api/voice"),
        apiGet<ContextSnapshot>("/api/context"),
        apiGet<{ pending: Json[] }>("/api/permissions"),
      ]);

    let order: string[] = sessionOrderRef.current;

    if (sessionsResult.status === "fulfilled") {
      const rows = Array.isArray(sessionsResult.value.sessions)
        ? sessionsResult.value.sessions
        : [];
      const mapped: Record<string, ChatSession> = {};
      order = [];
      for (const row of rows) {
        const session = sessionFromApi(row);
        if (!session.id) continue;
        mapped[session.id] = session;
        order.push(session.id);
      }
      commitSessions((prev) => {
        const next = { ...mapped };
        // Keep optimistic chats that have not reached the database yet.
        for (const [id, session] of Object.entries(prev)) {
          if (!next[id] && session.status === "running") next[id] = session;
        }
        return next;
      });
      commitSessionOrder((prev) => [
        ...order,
        ...prev.filter((id) => !order.includes(id)),
      ]);
    }

    if (tasksResult.status === "fulfilled") {
      setRemote(
        Array.isArray(tasksResult.value.tasks)
          ? tasksResult.value.tasks
          : [],
      );
    }
    if (voiceResult.status === "fulfilled") {
      setVoice(normalizeVoice(voiceResult.value));
    }
    if (contextResult.status === "fulfilled") {
      setContext(contextResult.value);
    }
    if (permsResult.status === "fulfilled" && !confirmRef.current) {
      const pending = Array.isArray(permsResult.value.pending)
        ? permsResult.value.pending
        : [];
      const first = pending[0];
      if (first) {
        const created = toMs(first.created);
        const expires = toMs(first.expires_at);
        setConfirmState({
          confirmationId: String(first.confirmation_id || ""),
          tier: String(first.tier || ""),
          tool: String(first.tool || ""),
          args:
            first.args && typeof first.args === "object"
              ? (first.args as Record<string, unknown>)
              : {},
          utterance: first.utterance
            ? String(first.utterance)
            : undefined,
          message: first.message ? String(first.message) : undefined,
          createdAt: created || Date.now(),
          expiresAt: expires || undefined,
        });
      }
    }

    let target = activeSessionIdRef.current;
    if (!target && order.length) {
      target = order[0];
      setActiveSession(target);
    }
    if (target) void hydrateSession(target);
  }, [
    commitSessionOrder,
    commitSessions,
    hydrateSession,
    setActiveSession,
    setConfirmState,
  ]);

  const run = useCallback(
    (goal: string, parentId = "") => {
      const text = goal.trim();
      if (!text) return;

      if (isDemoRef.current) {
        setConfirmState(null);
        const now = Date.now();
        commitTasks((prev) => {
          const current = prev.task_demo || demoTask();
          return {
            ...prev,
            task_demo: {
              ...current,
              goal: text,
              status: "running",
              live: true,
              startedAt: now,
              createdAt: current.createdAt || now,
              updatedAt: now,
              answer: "",
              error: "",
              needsInput: false,
              question: undefined,
              options: undefined,
              steps: [
                {
                  id: `demo-run-${now}`,
                  skill: "browser_search",
                  args: { query: text },
                  status: "running",
                  startedAt: now,
                },
              ],
              thinking: [],
            },
          };
        });
        return;
      }

      let sessionId = activeSessionIdRef.current;
      if (!sessionId) {
        sessionId = newSessionId();
        setActiveSession(sessionId);
      }
      ensureSessionForTask(sessionId, text, "", Date.now());

      const socket = wsRef.current;
      if (socket && socket.readyState === WebSocket.OPEN) {
        pendingRunsRef.current.push({ parentId, goal: text, sessionId });
        try {
          socket.send(
            JSON.stringify({
              action: "execute",
              command: text,
              session_id: sessionId,
            }),
          );
          return;
        } catch {
          pendingRunsRef.current.pop();
        }
      }

      // Socket not ready: the REST endpoint still runs the task; the event bus
      // will stream it to us as soon as the socket reconnects.
      void apiPost<Json>("/api/execute", {
        command: text,
        session_id: sessionId,
      })
        .then(() => {
          if (!parentId) return;
          void apiGet<{ tasks: SidecarTask[] }>(
            `/api/tasks?session_id=${encodeURIComponent(sessionId)}&limit=10`,
          )
            .then((data) => {
              const match = [...(data.tasks || [])]
                .reverse()
                .find((task) => task.goal === text);
              if (!match) return;
              pendingParentRef.current[match.id] = parentId;
              writeParent(match.id, parentId);
              commitTasks((prev) =>
                prev[match.id]
                  ? {
                      ...prev,
                      [match.id]: { ...prev[match.id], parentId },
                    }
                  : prev,
              );
            })
            .catch(() => {});
        })
        .catch((error) => {
          log("error", "Could not start task", String(error));
          setConnection("offline");
        });
    },
    [
      commitTasks,
      ensureSessionForTask,
      log,
      setActiveSession,
      setConfirmState,
    ],
  );

  const cancel = useCallback(() => {
    if (isDemoRef.current) {
      setConfirmState(null);
      commitTasks((prev) => {
        const current = prev.task_demo;
        if (!current) return prev;
        return {
          ...prev,
          task_demo: {
            ...current,
            status: "cancelled",
            live: false,
            updatedAt: Date.now(),
          },
        };
      });
      return;
    }
    const running = Object.values(tasksRef.current)
      .filter((task) => task.live && task.status === "running")
      .sort((a, b) => b.updatedAt - a.updatedAt)[0];
    const taskId = running?.id || confirmRef.current?.taskId || "";
    const socket = wsRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ action: "cancel", task_id: taskId }));
    }
    if (running) {
      commitTasks((prev) =>
        prev[running.id]
          ? {
              ...prev,
              [running.id]: {
                ...prev[running.id],
                status: "cancelled",
                live: false,
                updatedAt: Date.now(),
              },
            }
          : prev,
      );
      markSessionForTask(running.id, "cancelled");
    }
    if (confirmRef.current) setConfirmState(null);
  }, [
    commitTasks,
    markSessionForTask,
    setConfirmState,
  ]);

  const respond = useCallback(
    async (approve: boolean) => {
      const current = confirmRef.current;
      if (!current) return;
      setConfirmState(null);

      if (isDemoRef.current) {
        if (current.taskId) {
          commitTasks((prev) =>
            prev[current.taskId!]
              ? {
                  ...prev,
                  [current.taskId!]: {
                    ...prev[current.taskId!],
                    status: approve ? "completed" : "cancelled",
                    live: false,
                    needsInput: false,
                    answer: approve
                      ? "Approved. The summary was written to your Desktop."
                      : "Action denied.",
                    updatedAt: Date.now(),
                  },
                }
              : prev,
          );
        }
        return;
      }

      try {
        const response = await apiPost<Json>("/api/confirm", {
          confirmation_id: current.confirmationId,
          approve,
          remember: approve,
        });
        if (current.taskId) {
          if (approve) {
            if (response?.result) {
              finalizeFromExecution(current.taskId, response.result);
            }
          } else {
            commitTasks((prev) =>
              prev[current.taskId!]
                ? {
                    ...prev,
                    [current.taskId!]: {
                      ...prev[current.taskId!],
                      status: "cancelled",
                      live: false,
                      answer: "Action denied.",
                      updatedAt: Date.now(),
                    },
                  }
                : prev,
            );
            markSessionForTask(current.taskId, "cancelled");
          }
        }
        void refresh();
      } catch (error) {
        setConfirmState(current);
        log("error", "Confirmation failed", String(error));
      }
    },
    [
      commitTasks,
      finalizeFromExecution,
      log,
      markSessionForTask,
      refresh,
      setConfirmState,
    ],
  );

  const newChat = useCallback(() => {
    const id = newSessionId();
    setActiveSession(id);
    commitSessions((prev) => ({
      ...prev,
      [id]: {
        id,
        title: "New chat",
        createdAt: Date.now(),
        updatedAt: Date.now(),
        status: "idle",
        taskIds: [],
        taskCount: 0,
      },
    }));
    commitSessionOrder((prev) => [id, ...prev.filter((item) => item !== id)]);
  }, [commitSessionOrder, commitSessions, setActiveSession]);

  const selectSession = useCallback(
    (id: string) => {
      if (!id) return;
      setActiveSession(id);
      void hydrateSession(id);
    },
    [hydrateSession, setActiveSession],
  );

  const select = useCallback(
    (taskId: string) => {
      const task = tasksRef.current[taskId];
      if (!task) return;
      if (task.sessionId && task.sessionId !== activeSessionIdRef.current) {
        selectSession(task.sessionId);
      }
    },
    [selectSession],
  );

  const deleteChat = useCallback(
    async (id: string) => {
      if (!id) return;
      if (!isDemoRef.current) {
        try {
          await apiDelete<Json>(`/api/sessions/${encodeURIComponent(id)}`);
        } catch (error) {
          log("warn", "Could not delete chat", String(error));
        }
      }
      commitSessions((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      commitSessionOrder((prev) => prev.filter((item) => item !== id));
      commitTasks((prev) => {
        const next = { ...prev };
        for (const [taskId, task] of Object.entries(next)) {
          if (task.sessionId === id) delete next[taskId];
        }
        return next;
      });
      if (activeSessionIdRef.current === id) {
        const nextId = sessionOrderRef.current.find((item) => item !== id) || "";
        setActiveSession(nextId);
        if (nextId) void hydrateSession(nextId);
      }
    },
    [
      commitSessionOrder,
      commitSessions,
      commitTasks,
      hydrateSession,
      log,
      setActiveSession,
    ],
  );

  const voiceAction = useCallback(
    async (action: string) => {
      if (isDemoRef.current) {
        setVoice((prev) => {
          if (action === "mute") {
            return { ...prev, muted: true, phase: "idle", level: 0 };
          }
          if (action === "unmute") {
            return {
              ...prev,
              muted: false,
              phase: prev.running ? "listening" : "off",
            };
          }
          if (action === "stop") {
            return {
              ...prev,
              running: false,
              phase: "off",
              level: 0,
              partial: "",
            };
          }
          if (action === "interrupt") {
            return {
              ...prev,
              phase: prev.running ? "listening" : "idle",
              spoken: "",
            };
          }
          if (action === "start" || action === "start-wake" || action === "ptt") {
            return {
              ...prev,
              running: true,
              muted: false,
              phase: "listening",
            };
          }
          return prev;
        });
        return;
      }
      try {
        const data = await apiPost<Json>(`/api/voice/${action}`);
        setVoice(normalizeVoice(data));
        if (data?.error) {
          log("warn", "Voice", String(data.error));
        }
      } catch (error) {
        setVoice((prev) => ({ ...prev, error: String(error) }));
        log("error", "Voice action failed", String(error));
      }
    },
    [log],
  );

  const dismissConfirm = useCallback(() => {
    setConfirmState(null);
  }, [setConfirmState]);

  const clearActivity = useCallback(() => setActivity([]), []);
  const clearConnectionLog = useCallback(() => setConnectionLog([]), []);

  // Initial data + voice fallback polling.
  useEffect(() => {
    if (isDemo) return;
    void refresh();
    void loadVoice();
    const timer = window.setInterval(() => {
      void loadVoice();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [isDemo, loadVoice, refresh]);

  // WebSocket with a simple capped backoff.
  useEffect(() => {
    if (isDemo) return;
    let disposed = false;
    let reconnectTimer: number | null = null;
    let attempt = 0;

    const connect = () => {
      if (disposed) return;
      setConnection((prev) => (prev === "online" ? prev : "connecting"));
      let socket: WebSocket;
      try {
        socket = new WebSocket(SERVER_WS);
      } catch (error) {
        log("error", "Could not open WebSocket", String(error));
        setConnection("offline");
        return;
      }
      wsRef.current = socket;

      socket.onopen = () => {
        attempt = 0;
        if (disposed) {
          socket.close();
          return;
        }
        wsOpenRef.current = true;
        setConnection("online");
        log("ok", "WebSocket connected", SERVER_WS);
        // The sidecar may come up after the window: the first REST fetch can
        // fail while it boots, so pull history again once the socket is live.
        if (sessionOrderRef.current.length === 0) void refresh();
      };

      socket.onmessage = (event) => {
        try {
          handleMessage(String(event.data));
        } catch (error) {
          log("warn", "Ignored malformed event", String(error));
        }
      };

      socket.onerror = () => {
        log("error", "WebSocket error", SERVER_WS);
      };

      socket.onclose = () => {
        if (wsRef.current === socket) wsRef.current = null;
        wsOpenRef.current = false;
        if (disposed) return;
        setConnection("offline");
        attempt += 1;
        const delay = Math.min(1000 * 2 ** Math.min(attempt, 3), 8000);
        log(
          "warn",
          "WebSocket disconnected",
          `reconnecting in ${Math.round(delay / 1000)}s`,
        );
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      const socket = wsRef.current;
      wsRef.current = null;
      wsOpenRef.current = false;
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [handleMessage, isDemo, log, refresh]);

  // Electron main-process signals: sidecar lifecycle/logs, tray voice actions.
  useEffect(() => {
    if (isDemo) return;
    const api = window.winbrow;
    if (!api?.onMainEvent) return;

    const offLog = api.onMainEvent("sidecar:log", (payload) => {
      if (!payload || typeof payload !== "object") return;
      const entry = payload as ConnectionLogEntry;
      if (!entry.id) return;
      pushConnectionLog({
        ...entry,
        ts: Number(entry.ts) || Date.now(),
        detail: entry.detail || "",
      });
    });

    const offStatus = api.onMainEvent("sidecar:status", (state) => {
      const next = String(state || "");
      if (next === "offline") {
        // Ignore a failed probe while the WebSocket is still open: the probe
        // can time out under load, and the socket is the real data path.
        if (wsOpenRef.current) {
          const now = Date.now();
          if (now - lastHealthWarnRef.current > 60000) {
            lastHealthWarnRef.current = now;
            log(
              "warn",
              "Sidecar health probe failed",
              "WebSocket still connected; keeping the sidecar online",
            );
          }
          return;
        }
        setConnection("offline");
      } else if (next === "online") {
        setConnection((prev) => {
          if (wsOpenRef.current) return "online";
          return prev === "online" ? prev : "connecting";
        });
      }
    });

    const offError = api.onMainEvent("sidecar:error", (payload) => {
      const message =
        payload && typeof payload === "object" && "message" in payload
          ? String((payload as Json).message || "Sidecar error")
          : "Sidecar error";
      log("error", "Sidecar error", message, "error", "sidecar");
      setConnection("offline");
    });

    const offVoice = api.onMainEvent("voice:status", (payload) => {
      if (payload && typeof payload === "object") {
        setVoice(normalizeVoice(payload));
      }
    });

    return () => {
      offLog();
      offStatus();
      offError();
      offVoice();
    };
  }, [isDemo, log, pushConnectionLog]);

  const activeSession = activeSessionId
    ? sessions[activeSessionId]
    : undefined;

  const activeTasks = useMemo(() => {
    return Object.values(tasks)
      .filter((task) => task.sessionId === activeSessionId)
      .sort((a, b) => a.createdAt - b.createdAt);
  }, [activeSessionId, tasks]);

  const active = useMemo(() => {
    const all = Object.values(tasks);
    if (!all.length) return undefined;
    const running = all.filter(
      (task) => task.status === "running" && task.live,
    );
    if (running.length) {
      return running.reduce((a, b) => (a.updatedAt >= b.updatedAt ? a : b));
    }
    const waiting = all.filter((task) => task.needsInput);
    if (waiting.length) {
      return waiting.reduce((a, b) => (a.updatedAt >= b.updatedAt ? a : b));
    }
    return all.reduce((a, b) => (a.updatedAt >= b.updatedAt ? a : b));
  }, [tasks]);

  return {
    connection,
    tasks,
    remote,
    sessions,
    sessionOrder,
    activeSessionId,
    activeSession,
    activeTasks,
    active,
    confirm,
    context,
    voice,
    activity,
    connectionLog,
    demo: isDemo ? demo : undefined,
    run,
    cancel,
    newChat,
    selectSession,
    deleteChat,
    refresh,
    select,
    respond,
    refreshContext,
    dismissConfirm,
    voiceAction,
    clearActivity,
    clearConnectionLog,
  };
}
