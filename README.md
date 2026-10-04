# WinBrow 🤖 (Windows Desktop & Browser Control with Laya)

> Inspired by [macbrow](https://github.com/timpratim/macbrow), rebuilt specifically for **Windows 10/11**.

Talk to your Windows PC. Say a command and it runs as native **PowerShell** or drives your **browser**. Routing takes ~200ms because a **Laya Choice model chooses instead of generating**. When an unknown or complex task is requested, a dynamic generator writes a new PowerShell tool on the fly, checks it against safety policies, executes it, and persists it to `tools/learned.json` for ~150ms instant execution next time.

---

## ⚡ Architecture: Choose, Don't Generate

A typical computer-use agent runs a slow, fragile loop: *screenshot → LLM reasons → clicks coordinates*. Each turn takes 5-10 seconds, crashes in background sessions (e.g. `BitBlt` graphics errors), and hallucinates.

**WinBrow inverts this** (matching `macbrow`'s architecture):

```
User Voice / Text Utterance + Live Windows Context (Active App, Running Apps)
                               │
                               ▼
               Laya Typed Choice Router (~200ms)
                 │
                 ├─► Built-in Tool (Volume, Theme, Windows, Media, Apps) ─► Native PowerShell
                 │
                 ├─► Browser Task ─► Google Chrome / Microsoft Edge
                 │
                 ├─► Complex / Unseen Task ─► Dynamic Generator Tier (LLM writes PowerShell)
                 │                                │
                 │                                ├─► Validated & Policy-Checked (policy.py)
                 │                                ├─► Executed via PowerShell
                 │                                └─► Cached in tools/learned.json (~150ms next time!)
                 │
                 └─► Small Talk / Questions ─► One Terse Spoken Response
```

---

## 🌟 Key Capabilities

1. **Native Windows OS Control**
   - **System Volume & Mute**: Instant COM volume keys.
   - **Dark/Light Mode**: Windows Registry theme switcher (`HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize`).
   - **Media Playback**: Play/Pause, Next Track, Previous Track virtual key events.
   - **Window Management**: Minimize, maximize, snap left/right, show desktop.
   - **Desktop Organization**: Automatically organizes loose files into dated archive folders.
   - **Windows Settings Panes**: Direct `ms-settings:` deep linking (sound, wi-fi, display, bluetooth).

2. **Self-Learning Dynamic Tool Generator (`winbrow/generator.py`)**
   - Handles complex, arbitrary Windows tasks like:
     - `"show my wifi password"`
     - `"generate a battery health report"`
     - `"clean up temporary cache files"`
     - `"find all PDFs modified today"`
     - `"force close unresponsive app"`
   - Supports local models via **Ollama** (`http://localhost:11434`) or **LM Studio** (`http://localhost:1234/v1`), **OpenAI** (`gpt-4o-mini`), or the built-in smart synthesizer.
   - Saves working scripts into `tools/learned.json`.

3. **Browser Automation (`winbrow/browser_task.py`)**
   - Drives Google Chrome and Microsoft Edge directly.
   - Web searches (Google, YouTube, GitHub, Bing), tab navigation, and shortcuts without heavy screenshot loops.

4. **Safety Policy Engine (`winbrow/policy.py`)**
   - Blocks dangerous operations (disk formatting, system folder deletions, critical process termination).

5. **Compact HUD Web UI**
   - Designed specifically as a small floating widget that sits unobtrusively on your desktop.
   - Speech-to-Text microphone button for hands-free voice control.
   - Real-time Laya routing inspector showing tool choice, confidence %, and latency in milliseconds.
   - Live Windows Context banner (Frontmost App, Running Apps count).
   - "Learned Tools" browser displaying dynamically generated tools.

---

## 🚀 Quick Start

### 1. Launch Server

Double-click `start.bat` or run:

```bash
# Start server
py -3 -m uvicorn server:app --host 127.0.0.1 --port 8765
```

### 2. Open UI

Navigate to:
👉 **http://127.0.0.1:8765**

---

## 📋 Example Commands

| Command | What it does | Type |
|---|---|---|
| `turn on dark mode` | Switches Windows theme to Dark Mode | Built-in |
| `turn up the volume` | Increases master audio volume | Built-in |
| `mute the sound` | Mutes/unmutes audio | Built-in |
| `show my wifi password` | Extracts Wi-Fi security key for active network | Dynamic Tool |
| `check battery health report` | Runs powercfg diagnostic & opens report | Dynamic Tool |
| `clean up my desktop` | Tidies loose desktop files into dated archive folder | Built-in |
| `search google for latest AI news` | Opens Chrome/Edge and performs search | Browser Task |
| `open sound settings` | Opens Windows Sound Settings pane | Built-in |
| `minimize window` | Minimizes the active foreground window | Built-in |
| `clean temp files` | Removes temporary cache files from Windows Temp | Built-in |
| `what time is it` | Shows current time, date, day, and timezone | Built-in |
| `open downloads folder` | Opens Downloads folder in File Explorer | Built-in |
| `find files named report` | Searches Desktop/Documents/Downloads for matching files | Built-in |
| `set volume to 40%` | Sets Windows audio level directly to exact percentage | Built-in |
| `increase brightness` | Adjusts monitor/laptop screen brightness | Built-in |
| `take screenshot to clipboard` | Copies full screenshot to clipboard (ready to paste) | Built-in |
| `type Hello World` | Types text into active focused input field via clipboard paste | Built-in |
| `open calculator` | Launches Calculator, Paint, Terminal, Snipping Tool, Task Manager, etc. | Built-in |
| `system uptime` | Shows days/hours computer has been running since boot | Built-in |
| `find large files` | Scans user folders for largest space-consuming files | Built-in |
| `turn on night light` | Opens Night Light / blue light filter settings | Built-in |
| `show running apps` | Lists all open apps with window titles and memory usage | Built-in |
| `create a note` | Creates a timestamped text file on Desktop and opens in Notepad | Built-in |
| `open bluetooth settings` | Deep links to Windows Bluetooth device management | Built-in |
| `shutdown in 1 hour` | Schedules a Windows shutdown or restart timer | Built-in |
| `lock screen` | Secures PC immediately via Win32 LockWorkStation | Built-in |
| `what is my public ip` | Retrieves public IP address and copies to clipboard | Built-in |

