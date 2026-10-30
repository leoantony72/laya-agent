import type { TaskView } from "../types";

function pad(value: number): string {
  return String(Math.max(0, Math.floor(value))).padStart(2, "0");
}

/** Clock time for trace rows. Accepts milliseconds since the epoch. */
export function shortTime(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return "";
  return new Date(ts).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Compact "how long ago" label for the chat rail. */
export function relativeTime(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return "";
  const diff = Math.max(0, Date.now() - ts);
  const seconds = Math.floor(diff / 1000);
  if (seconds < 45) return "now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  return new Date(ts).toLocaleDateString([], { month: "short", day: "numeric" });
}

function duration(ms: number): string {
  const total = Math.max(0, ms);
  if (total < 1000) return `${Math.round(total)}ms`;
  const seconds = total / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  if (minutes < 60) return `${minutes}m ${pad(rest)}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${pad(minutes % 60)}m`;
}

/** Elapsed time for a task, frozen once the task is no longer live. */
export function elapsed(task?: TaskView | null): string {
  if (!task) return "";
  const start = task.startedAt || task.createdAt;
  if (!Number.isFinite(start) || start <= 0) return "";
  const end =
    task.live || task.status === "running"
      ? Date.now()
      : task.updatedAt || Date.now();
  return duration(Math.max(0, end - start));
}

function clip(value: string, limit: number): string {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

const ARG_PRIORITY = [
  "path",
  "file",
  "filepath",
  "filename",
  "url",
  "query",
  "q",
  "text",
  "command",
  "app",
  "title",
  "name",
  "goal",
  "selector",
  "subject",
  "to",
];

function argValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return clip(value, 90);
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return clip(JSON.stringify(value), 90);
  } catch {
    return String(value);
  }
}

/** One-line hint for a tool call's arguments (used in trace + confirm bars). */
export function argHint(args?: Record<string, unknown> | null): string {
  if (!args || typeof args !== "object" || Array.isArray(args)) return "";
  const keys = Object.keys(args);
  if (!keys.length) return "";
  const ordered = [
    ...ARG_PRIORITY.filter((key) => keys.includes(key)),
    ...keys.filter((key) => !ARG_PRIORITY.includes(key)),
  ];
  const parts: string[] = [];
  for (const key of ordered) {
    const value = argValue(args[key]);
    if (!value) continue;
    parts.push(`${key}=${value}`);
    if (parts.length >= 3) break;
  }
  return clip(parts.join(" · "), 180);
}

const VERBS: Array<[RegExp, string]> = [
  [/search|browse|lookup|look_up/, "Searching the web"],
  [/read|document|pdf|ocr|extract/, "Reading"],
  [/write|create|edit|save|document_/, "Writing"],
  [/download|fetch|curl/, "Downloading"],
  [/screenshot|capture_screen/, "Taking a screenshot"],
  [/computer_|click|type_text|key_press|scroll|paste|drag/, "Controlling the computer"],
  [/open|launch|focus_window|minimize/, "Opening"],
  [/run_command|powershell|shell|terminal/, "Running a command"],
  [/browser|navigate|tab_/, "Using the browser"],
  [/email|mail|gmail/, "Sending email"],
  [/image|generate_image|draw/, "Generating an image"],
  [/weather|forecast/, "Checking the weather"],
  [/calendar|schedule|reminder/, "Checking your calendar"],
  [/install|winget/, "Installing"],
  [/file|folder|directory|glob|find/, "Looking through files"],
  [/delegate|subagent|agent/, "Delegating"],
];

/** Human verb for a skill key ("browser_search" → "Searching the web"). */
export function skillVerb(skill?: string | null): string {
  const key = String(skill || "").trim().toLowerCase();
  if (!key) return "Thinking";
  for (const [pattern, verb] of VERBS) {
    if (pattern.test(key)) return verb;
  }
  return "Working";
}

/** Compact clock for the island ("0:12", "2:05"). */
export function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${pad(seconds)}`;
}

/** First line of a longer answer, clipped for the island. */
export function snippet(text?: string | null, limit = 180): string {
  const value = String(text || "").trim();
  if (!value) return "";
  const line = value.split(/\r?\n/).find((part) => part.trim()) || value;
  return clip(line.replace(/^#+\s*/, "").replace(/[*_`>]/g, ""), limit);
}
