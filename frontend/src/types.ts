export type ConnectionState = "connecting" | "online" | "offline";

export type StepStatus = "running" | "success" | "failed" | "skipped" | "cancelled";

export interface TraceStep {
  id: string;
  skill: string;
  args: Record<string, unknown>;
  why?: string;
  status: StepStatus;
  output?: string;
  warning?: string;
  elapsedMs?: number;
  startedAt: number;
  endedAt?: number;
}

export interface ThinkingBlock {
  id: string;
  kind: "summary" | "raw";
  text: string;
  ts: number;
}

export interface ConfirmRequest {
  confirmationId: string;
  tier: string;
  tool: string;
  args: Record<string, unknown>;
  utterance?: string;
  message?: string;
  createdAt?: number;
  expiresAt?: number;
}

export type VoicePhase = "off" | "idle" | "listening" | "thinking" | "speaking";

export interface VoiceState {
  running: boolean;
  mode: string;
  muted: boolean;
  error: string;
  phase: VoicePhase;
  level: number;
  partial: string;
  transcript: string;
  spoken: string;
  busy: boolean;
}

export interface ContextSnapshot {
  active_app: string;
  active_title: string;
  volume_level?: number;
  is_muted?: boolean;
}

export type ConnectionLogLevel = "info" | "ok" | "warn" | "error";
export type ConnectionLogSource = "host" | "sidecar" | "ui";
export type ConnectionLogKind = "connection" | "ping" | "error";

export interface ConnectionLogEntry {
  id: string;
  ts: number;
  level: ConnectionLogLevel;
  source: ConnectionLogSource;
  kind: ConnectionLogKind;
  message: string;
  detail?: string;
}

/** One line in the Server activity page: brain thoughts, steps, lifecycle. */
export type ActivityKind = "task" | "thinking" | "step" | "answer" | "confirm" | "server";
export type ActivityStatus = "running" | "success" | "failed" | "info";

export interface ActivityEntry {
  id: string;
  /** Milliseconds since epoch. */
  ts: number;
  kind: ActivityKind;
  status: ActivityStatus;
  taskId?: string;
  title: string;
  body?: string;
  detail?: string;
  elapsedMs?: number;
}

export interface ChatSession {
  id: string; // session_id
  title: string;
  createdAt: number;
  updatedAt: number;
  status: string;
  taskIds: string[];
  taskCount?: number;
}

export interface TaskView {
  id: string;
  sessionId?: string;
  goal: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  createdAt: number;
  updatedAt: number;
  thinking: ThinkingBlock[];
  steps: TraceStep[];
  answer: string;
  summary?: string;
  error?: string;
  success?: boolean;
  live: boolean;
  startedAt?: number;
  /** Set when this task was asked as a follow-up from another task. */
  parentId?: string;
  /** The agent stopped to ask the user a question instead of finishing. */
  needsInput?: boolean;
  question?: string;
  options?: string[];
  evidence?: Record<string, unknown>;
}

export interface Envelope {
  v: number;
  type: string;
  task_id: string;
  step_id: string;
  seq: number;
  ts: number;
  payload: Record<string, any>;
}

export interface SidecarTask {
  id: string;
  sessionId?: string;
  session_id?: string;
  goal: string;
  status: string;
  created_at: number;
  updated_at: number;
  step_count?: number;
  summary?: string;
}

declare global {
  interface Window {
    winbrow?: {
      serverUrl: string;
      resizeIsland: (width: number, height: number) => void;
      focusIsland: () => void;
      blurIsland: () => void;
      openDashboard: () => void;
      closeDashboard: () => void;
      minimize: () => void;
      maximize: () => void;
      startSidecar: () => void;
      restartSidecar: () => void;
      getSidecarLog: () => Promise<{
        entries: ConnectionLogEntry[];
        path: string;
      }>;
      quit: () => void;
      onMainEvent: (
        channel: string,
        handler: (payload: any) => void,
      ) => () => void;
    };
  }
}
