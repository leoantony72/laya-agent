const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("winbrow", {
  // Where the renderer should connect (main may point at a non-default port).
  serverUrl: process.env.WINBROW_SERVER_URL || "http://127.0.0.1:8765",

  // Island window controls
  resizeIsland: (width, height) =>
    ipcRenderer.send("island:resize", { width, height }),
  // Only the text input asks for focus, so the island never steals it from
  // the app you are typing in just by appearing.
  focusIsland: () => ipcRenderer.send("island:focus"),
  blurIsland: () => ipcRenderer.send("island:blur"),

  // Dashboard window controls
  openDashboard: () => ipcRenderer.send("dashboard:open"),
  closeDashboard: () => ipcRenderer.send("dashboard:close"),
  minimize: () => ipcRenderer.send("dashboard:minimize"),
  maximize: () => ipcRenderer.send("dashboard:maximize"),
  startSidecar: () => ipcRenderer.send("sidecar:start"),
  restartSidecar: () => ipcRenderer.send("sidecar:restart"),
  getSidecarLog: () => ipcRenderer.invoke("sidecar:log:get"),
  quit: () => ipcRenderer.send("app:quit"),

  // Main → renderer signals
  onMainEvent: (channel, handler) => {
    const allowed = [
      "main:kill",
      "main:alert",
      "main:toggle-island",
      "main:show-server",
      "sidecar:status",
      "sidecar:error",
      "sidecar:log",
      "voice:status",
    ];
    if (!allowed.includes(channel)) return () => {};
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  },
});
