const {
  app,
  BrowserWindow,
  ipcMain,
  screen,
  Tray,
  Menu,
  globalShortcut,
  nativeImage,
  shell,
} = require("electron");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");

const SERVER_URL = process.env.WINBROW_SERVER_URL || "http://127.0.0.1:8765";
const DEV_URL = process.env.VITE_DEV_SERVER_URL || "";
const IS_DEV = Boolean(DEV_URL);

const ISLAND_WIDTH = 208;
const ISLAND_HEIGHT = 44;
const ISLAND_MARGIN_TOP = 12;

let island = null;
let dashboard = null;
let tray = null;
let sidecar = null;
let sidecarState = "connecting";
// A single slow probe must not flip the badge: the machine can be saturated by
// a task (browser automation, model streams) while the sidecar is healthy.
let healthFailures = 0;
let islandInteractive = false;
let islandHitTimer = null;

const CONNECTION_LOG_LIMIT = 500;
let connectionLog = [];
let connectionSeq = 0;
let connectionLogPath = "";

// ---------------------------------------------------------------- helpers
function rendererUrl(win) {
  const query = `?win=${win}`;
  if (IS_DEV) return `${DEV_URL}/${query}`;
  return `file://${path
    .join(__dirname, "..", "dist", "index.html")
    .replace(/\\/g, "/")}${query}`;
}

function primaryWorkArea() {
  const display = screen.getPrimaryDisplay();
  return display.workArea;
}

function placeIsland(width = ISLAND_WIDTH, height = ISLAND_HEIGHT) {
  if (!island) return;
  const area = primaryWorkArea();
  const x = Math.round(area.x + (area.width - width) / 2);
  const y = Math.round(area.y + ISLAND_MARGIN_TOP);
  island.setBounds({ x, y, width: Math.round(width), height: Math.round(height) });
}

// Click-through must be decided by the main process, not by DOM mouseenter:
// while a window ignores mouse events, forwarded DOM events are not reliable
// enough to re-arm it. Poll the real cursor against the island bounds instead.
function startIslandHitTest() {
  if (islandHitTimer) return;
  islandHitTimer = setInterval(() => {
    if (!island || island.isDestroyed() || !island.isVisible()) return;
    const point = screen.getCursorScreenPoint();
    const bounds = island.getBounds();
    const inside =
      point.x >= bounds.x &&
      point.x < bounds.x + bounds.width &&
      point.y >= bounds.y &&
      point.y < bounds.y + bounds.height;
    if (inside === islandInteractive) return;
    islandInteractive = inside;
    island.setIgnoreMouseEvents(!inside, { forward: true });
    if (process.env.WINBROW_ISLAND_DEBUG) {
      console.log(`[island] hit-test interactive=${inside}`);
    }
  }, 80);
}

function createIsland() {
  island = new BrowserWindow({
    width: ISLAND_WIDTH,
    height: ISLAND_HEIGHT,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    focusable: false,
    alwaysOnTop: true,
    show: false,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  island.setAlwaysOnTop(true, "screen-saver");
  // Do not draw over fullscreen video/games: that is interference, not utility.
  island.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: false });
  island.setIgnoreMouseEvents(true, { forward: true });
  island.setFocusable(false);
  island.loadURL(rendererUrl("island"));
  island.once("ready-to-show", () => {
    placeIsland();
    island.showInactive();
    island.setAlwaysOnTop(true, "screen-saver");
  });
  // A focused island (text input) gives focus back the moment it is left.
  island.on("blur", () => {
    if (island && !island.isDestroyed()) island.setFocusable(false);
  });
  island.on("closed", () => {
    island = null;
  });
}

function createDashboard() {
  dashboard = new BrowserWindow({
    width: 1180,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    frame: false,
    show: false,
    backgroundColor: "#0b0d10",
    title: "WinBrow",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  dashboard.loadURL(rendererUrl("dashboard"));
  dashboard.on("closed", () => {
    dashboard = null;
  });
  dashboard.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
}

function showDashboard() {
  if (!dashboard) createDashboard();
  if (!dashboard.isVisible()) dashboard.show();
  dashboard.focus();
}

function toggleDashboard() {
  if (dashboard && dashboard.isVisible()) {
    dashboard.hide();
  } else {
    showDashboard();
  }
}

// Sending to a window whose render frame is mid-navigation (Vite HMR reload,
// sidecar restart) throws "Render frame was disposed". The frame comes back on
// its own and the renderer re-syncs over REST/WebSocket, so a dropped message
// here is harmless.
function sendTo(win, channel, payload) {
  if (!win || win.isDestroyed()) return;
  const contents = win.webContents;
  if (!contents || contents.isDestroyed()) return;
  try {
    contents.send(channel, payload);
  } catch {
    // Frame disposed; ignore.
  }
}

function showServerActivity() {
  showDashboard();
  const win = dashboard;
  if (!win || win.isDestroyed()) return;
  const contents = win.webContents;
  if (!contents || contents.isDestroyed()) return;
  const send = () => sendTo(win, "main:show-server", {});
  if (contents.isLoading()) {
    contents.once("did-finish-load", send);
    return;
  }
  send();
}

function broadcast(channel, payload) {
  sendTo(island, channel, payload);
  sendTo(dashboard, channel, payload);
}

// ---------------------------------------------------------------- connection log
// A small, user-facing log of lifecycle/health/WebSocket events. API request
// lines are deliberately filtered out; the raw sidecar log stays on disk for
// deep debugging.
function connectionLogFile() {
  const dir = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, "connection.jsonl");
}

function loadConnectionLog() {
  connectionLogPath = connectionLogFile();
  // The Server activity page is a live view of this run. Previous sessions stay
  // in the file for post-mortem debugging but are not replayed into the UI.
  try {
    if (fs.statSync(connectionLogPath).size > 512 * 1024) {
      fs.writeFileSync(connectionLogPath, "", "utf8");
    }
  } catch {
    // First run: no log yet.
  }
}

function persistConnectionLog(entry) {
  if (!connectionLogPath) return;
  try {
    fs.appendFileSync(connectionLogPath, `${JSON.stringify(entry)}\n`, "utf8");
    if (connectionLog.length % 200 === 0) {
      if (fs.statSync(connectionLogPath).size > 512 * 1024) {
        const keep = connectionLog.slice(-CONNECTION_LOG_LIMIT)
          .map((item) => JSON.stringify(item))
          .join("\n");
        fs.writeFileSync(connectionLogPath, `${keep}\n`, "utf8");
      }
    }
  } catch {
    // Logging must never break the app.
  }
}

function logConnection(level, message, detail = "", kind = "connection", source = "host") {
  const entry = {
    id: `host-${Date.now()}-${++connectionSeq}`,
    ts: Date.now(),
    level,
    source,
    kind,
    message: String(message || "").slice(0, 200),
    detail: String(detail || "").slice(0, 800),
  };
  connectionLog.push(entry);
  if (connectionLog.length > CONNECTION_LOG_LIMIT) {
    connectionLog = connectionLog.slice(-CONNECTION_LOG_LIMIT);
  }
  broadcast("sidecar:log", entry);
  if (kind !== "ping") persistConnectionLog(entry);
}

const CONNECTION_LINE = /(Started server process|Waiting for application startup|Application startup complete|Uvicorn running|Shutting down|Finished server process)/i;
const API_LINE = /\b(GET|POST|PUT|PATCH|DELETE)\s+\/(api|ws)\b|\b127\.0\.0\.1\b.*\b(GET|POST|PUT|PATCH|DELETE)\b/i;
const SIDECAR_ERROR_LINE = /(traceback|exception|critical|fatal|\berror\b)/i;

function handleSidecarLine(raw, isErr) {
  const line = String(raw || "").replace(/\x1b\[[0-9;]*m/g, "").trim();
  if (!line) return;
  // Never surface request logs; that is the "don't show the API" rule.
  if (API_LINE.test(line)) return;
  if (isErr && SIDECAR_ERROR_LINE.test(line) && !CONNECTION_LINE.test(line)) {
    logConnection("error", "Sidecar error", line, "error", "sidecar");
    return;
  }
  const match = line.match(CONNECTION_LINE);
  if (!match) return;
  const ok = /(startup complete|Uvicorn running)/i.test(line);
  logConnection(
    ok ? "ok" : "info",
    `Sidecar: ${match[1]}`,
    "",
    "connection",
    "sidecar",
  );
}

function watchSidecarStream(stream, isErr) {
  let buffer = "";
  stream.on("data", (chunk) => {
    buffer += chunk.toString("utf8");
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      handleSidecarLine(line, isErr);
    }
    if (buffer.length > 8192) buffer = buffer.slice(-8192);
  });
}

// ---------------------------------------------------------------- sidecar
function health(onResult) {
  let settled = false;
  const done = (ok) => {
    if (settled) return;
    settled = true;
    onResult(ok);
  };
  // /api/health is intentionally trivial (no imports, no executor, no store),
  // so a timeout here means the event loop itself is stuck.
  const req = http.get(`${SERVER_URL}/api/health`, { timeout: 3000 }, (res) => {
    res.resume();
    done(res.statusCode === 200);
  });
  req.on("error", () => done(false));
  req.on("timeout", () => {
    req.destroy();
    done(false);
  });
}

function setSidecarState(state) {
  if (sidecarState === state) return;
  sidecarState = state;
  broadcast("sidecar:status", state);
}

function spawnSidecar() {
  if (sidecar && !sidecar.killed) return;
  const packaged = path.join(process.resourcesPath || "", "backend", "winbrow-agent.exe");
  const devRoot = path.resolve(__dirname, "..", "..");
  const venvPython = path.join(devRoot, ".venv", "Scripts", "python.exe");
  const python =
    process.env.WINBROW_PYTHON ||
    (fs.existsSync(venvPython) ? venvPython : "python");
  let serverPort = "8765";
  try {
    serverPort = new URL(SERVER_URL).port || "8765";
  } catch {
    serverPort = "8765";
  }

  const logDir = path.join(devRoot, "logs");
  let logStream = null;
  try {
    fs.mkdirSync(logDir, { recursive: true });
    logStream = fs.createWriteStream(
      path.join(logDir, "desktop-sidecar.log"),
      { flags: "a" },
    );
    logStream.write(
      `\n[${new Date().toISOString()}] starting sidecar\n`,
    );
  } catch {
    logStream = null;
  }

  const command = fs.existsSync(packaged)
    ? `${packaged} --port ${serverPort}`
    : `${python} -m uvicorn server:app --host 127.0.0.1 --port ${serverPort}`;
  logConnection("info", "Starting Python sidecar", command, "connection");

  if (fs.existsSync(packaged)) {
    sidecar = spawn(packaged, ["--port", serverPort], {
      cwd: path.dirname(packaged),
      stdio: logStream ? ["ignore", "pipe", "pipe"] : "ignore",
      windowsHide: true,
    });
  } else {
    sidecar = spawn(
      python,
      ["-m", "uvicorn", "server:app", "--host", "127.0.0.1",
       "--port", serverPort],
      {
        cwd: devRoot,
        stdio: logStream ? ["ignore", "pipe", "pipe"] : "ignore",
        windowsHide: true,
      },
    );
  }

  const child = sidecar;
  if (child.stdout) watchSidecarStream(child.stdout, false);
  if (child.stderr) watchSidecarStream(child.stderr, true);
  if (logStream && child.stdout && child.stderr) {
    child.stdout.pipe(logStream);
    child.stderr.pipe(logStream);
  }
  child.on("error", (err) => {
    if (sidecar === child) setSidecarState("offline");
    logConnection(
      "error",
      "Could not start the Python sidecar",
      String(err.code || err.message || err),
      "error",
    );
    broadcast("sidecar:error", {
      message: `Could not start the Python sidecar (${err.code || err.message}).`,
    });
  });
  child.on("exit", (code) => {
    const wasCurrent = sidecar === child;
    if (wasCurrent) {
      sidecar = null;
      setSidecarState("offline");
    }
    if (code && code !== 0) {
      logConnection(
        "error",
        "Sidecar exited",
        `code ${code}${wasCurrent ? "" : " · old process"} · see logs/desktop-sidecar.log`,
        "error",
      );
      broadcast("sidecar:error", {
        message: `Sidecar exited with code ${code}. See logs/desktop-sidecar.log.`,
      });
    } else {
      logConnection(
        "warn",
        "Sidecar stopped",
        `code ${code ?? 0}${wasCurrent ? "" : " · old process"}`,
        "connection",
      );
    }
  });
}

function ensureSidecar() {
  health((ok) => {
    if (ok) {
      setSidecarState("online");
      return;
    }
    setSidecarState("offline");
    spawnSidecar();
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      health((up) => {
        if (up) {
          clearInterval(timer);
          logConnection("ok", "Python server ready", SERVER_URL, "connection");
          setSidecarState("online");
        } else if (tries >= 30) {
          clearInterval(timer);
          logConnection(
            "error",
            "Python server did not start",
            "gave up after 30s · see logs/desktop-sidecar.log",
            "error",
          );
          setSidecarState("offline");
        }
      });
    }, 1000);
  });
}

function voicePost(action) {
  postJson(`/api/voice/${action}`, (data) => {
    broadcast("voice:status", data || {});
    if (data && data.error) {
      broadcast("main:alert", { message: String(data.error) });
    }
  });
}

function postJson(route, onDone) {
  const url = new URL(`${SERVER_URL}${route}`);
  const req = http.request(
    {
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      method: "POST",
      timeout: 4000,
      headers: { "content-type": "application/json", "content-length": 0 },
    },
    (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          onDone && onDone(JSON.parse(body || "{}"));
        } catch {
          onDone && onDone({});
        }
      });
    },
  );
  req.on("error", () => onDone && onDone({ error: "sidecar offline" }));
  req.on("timeout", () => {
    req.destroy();
    onDone && onDone({ error: "timeout" });
  });
  req.end();
}

// ---------------------------------------------------------------- tray
function createTray() {
  const iconPath = path.join(__dirname, "tray.png");
  const image = fs.existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath)
    : nativeImage.createEmpty();
  tray = new Tray(image);
  tray.setToolTip("WinBrow");
  const menu = Menu.buildFromTemplate([
    { label: "Open dashboard", click: showDashboard },
    { label: "Server activity", click: showServerActivity },
    {
      label: "Show island",
      click: () => {
        if (island) {
          island.showInactive();
          placeIsland();
        } else {
          createIsland();
        }
      },
    },
    { type: "separator" },
    {
      label: "Voice: start / stop",
      click: () => voicePost("toggle"),
    },
    {
      label: "Voice: mute / unmute",
      click: () => voicePost("toggle-mute"),
    },
    {
      label: "Voice: push to talk",
      click: () => voicePost("ptt"),
    },
    { type: "separator" },
    { label: "Start sidecar", click: ensureSidecar },
    {
      label: "Start at login",
      type: "checkbox",
      checked: app.getLoginItemSettings().openAtLogin,
      click: (item) =>
        app.setLoginItemSettings({ openAtLogin: Boolean(item.checked) }),
    },
    { type: "separator" },
    {
      label: "Cancel current task",
      click: () => broadcast("main:kill", { reason: "tray" }),
    },
    { type: "separator" },
    { label: "Quit", click: () => app.quit() },
  ]);
  tray.setContextMenu(menu);
  tray.on("click", toggleDashboard);
}

// ---------------------------------------------------------------- ipc
ipcMain.on("island:resize", (_event, size) => {
  if (!island || !size) return;
  const width = Math.min(Math.max(Number(size.width) || ISLAND_WIDTH, 160), 720);
  const height = Math.min(Math.max(Number(size.height) || ISLAND_HEIGHT, 30), 560);
  placeIsland(width, height);
});

// Focus is opt-in: only the island's text input asks for it.
ipcMain.on("island:focus", () => {
  if (!island || island.isDestroyed()) return;
  island.setFocusable(true);
  island.focus();
});

ipcMain.on("island:blur", () => {
  if (!island || island.isDestroyed()) return;
  island.blur();
  island.setFocusable(false);
});

ipcMain.on("dashboard:open", showDashboard);
ipcMain.on("dashboard:close", () => dashboard && dashboard.hide());
ipcMain.on("dashboard:minimize", () => dashboard && dashboard.minimize());
ipcMain.on("dashboard:maximize", () => {
  if (!dashboard) return;
  dashboard.isMaximized() ? dashboard.unmaximize() : dashboard.maximize();
});
ipcMain.on("sidecar:start", ensureSidecar);
ipcMain.handle("sidecar:log:get", () => ({
  entries: connectionLog,
  path: connectionLogPath,
}));
ipcMain.on("sidecar:restart", () => {
  logConnection("info", "Restart requested", "", "connection");
  if (!sidecar || sidecar.killed) {
    ensureSidecar();
    return;
  }
  const pid = sidecar.pid;
  sidecar = null;
  try {
    spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
  } catch {
    // The exit handler will still report the old process.
  }
  setSidecarState("offline");
  setTimeout(ensureSidecar, 1600);
});
ipcMain.on("app:quit", () => app.quit());

function isFirstRun() {
  try {
    const marker = path.join(app.getPath("userData"), "first-run.done");
    if (fs.existsSync(marker)) return false;
    fs.mkdirSync(path.dirname(marker), { recursive: true });
    fs.writeFileSync(marker, new Date().toISOString());
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- lifecycle
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", showDashboard);

  app.whenReady().then(() => {
    loadConnectionLog();
    createIsland();
    startIslandHitTest();
    createDashboard();
    createTray();
    ensureSidecar();
    if (isFirstRun()) showDashboard();

    globalShortcut.register("CommandOrControl+Alt+Space", toggleDashboard);
    globalShortcut.register("CommandOrControl+Alt+I", () => {
      broadcast("main:toggle-island", {});
    });
    globalShortcut.register("CommandOrControl+Alt+Escape", () => {
      broadcast("main:kill", { reason: "hotkey" });
      broadcast("main:alert", { message: "Stopped" });
    });
    // Voice controls stay reachable without focusing the island.
    globalShortcut.register("CommandOrControl+Alt+T", () => voicePost("ptt"));
    globalShortcut.register("CommandOrControl+Alt+M", () => voicePost("toggle-mute"));
    globalShortcut.register("CommandOrControl+Alt+X", () => voicePost("interrupt"));

    screen.on("display-metrics-changed", () => {
      if (island) {
        const [w, h] = island.getSize();
        placeIsland(w, h);
      }
    });
    screen.on("display-added", () => island && placeIsland());
    screen.on("display-removed", () => island && placeIsland());

    setInterval(() => {
      health((ok) => {
        if (ok) {
          healthFailures = 0;
          setSidecarState("online");
          return;
        }
        healthFailures += 1;
        // Two misses (~30s) is a real outage, not a transient stall.
        if (healthFailures >= 2) setSidecarState("offline");
      });
    }, 15000);
  });

  app.on("will-quit", () => {
    globalShortcut.unregisterAll();
    if (islandHitTimer) {
      clearInterval(islandHitTimer);
      islandHitTimer = null;
    }
    if (sidecar && !sidecar.killed) {
      if (process.platform === "win32") {
        spawn("taskkill", ["/PID", String(sidecar.pid), "/T", "/F"], {
          windowsHide: true,
        });
      } else {
        sidecar.kill();
      }
    }
  });

  // Keep the tray/island alive when the dashboard is closed.
  app.on("window-all-closed", () => {});
}
