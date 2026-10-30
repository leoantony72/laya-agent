// Connection details and tiny REST helpers shared by the dashboard and island.
//
// The Electron preload exposes `window.winbrow.serverUrl` (it may point at a
// non-default port). In a plain browser / Vite dev server we fall back to the
// sidecar's default address.

export const SERVER_HTTP = (
  window.winbrow?.serverUrl || "http://127.0.0.1:8765"
).replace(/\/+$/, "");

export const SERVER_WS = `${SERVER_HTTP.replace(/^http/i, "ws")}/ws`;

async function readError(response: Response): Promise<string> {
  try {
    const text = (await response.text()).trim();
    if (text) {
      // FastAPI returns JSON errors; prefer the `detail`/`error` field.
      try {
        const data = JSON.parse(text);
        const detail = data?.detail ?? data?.error;
        if (detail) return String(detail);
      } catch {
        // Not JSON: fall through to the raw body.
      }
      return text.slice(0, 300);
    }
  } catch {
    // Ignore body-read failures.
  }
  return `${response.status} ${response.statusText}`.trim();
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${SERVER_HTTP}${path}`, {
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(`${SERVER_HTTP}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as T;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(`${SERVER_HTTP}${path}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as T;
}
