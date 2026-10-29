# WinBrow Dynamic Island — state machine spec

The island is a glanceable status layer with a few quick actions. It shows **one
primary thing at a time**. Anything dense, scrollable, or historical belongs in
the dashboard; the island hands off through "Open dashboard" and the escalation
rules below.

Implementation: `src/lib/islandState.ts` (pure derivation) and `src/Island.tsx`
(rendering). The shell lives in `electron/main.cjs`; voice events come from
`winbrow/voice_service.py`.

## States

| State | Size | Primary content | Stays open? |
|---|---|---|---|
| `idle` | small (196×36) | mic indicator, "Ready", chevron | no |
| `listening` | medium | live waveform, partial transcript, mute | no (collapses after 8s idle) |
| `speaking` | medium | speaking indicator, reply snippet, stop-speaking | no |
| `thinking` | medium | spinner, goal | no |
| `working` | medium | status line, step `n/m`, progress bar, elapsed, cancel | no |
| `done` | small | "Done", check (6s hold) | no |
| `error` | medium | failure line, retry/open | no |
| `confirm` | large | tier, tool, args, expiry, Allow / Deny | **yes** |
| `control` | medium, high contrast | warning banner, target app, kill switch | **yes** |

`expanded` is a layout flag, not a state. Any state can open the large card
(conversation snippet, text input, quick actions, context chip); opening the
card does not change which state is primary.

## Priority (highest first)

1. **Confirmations** — the agent is blocked; a decision is required.
2. **Control mode** — the agent is driving the mouse/keyboard (kill switch).
3. **Errors** — the active task failed.
4. **Voice** — listening, then speaking.
5. **Task progress** — working / thinking.
6. **Done** — transient success/cancel notice (6s).
7. **Idle**.

A pending confirmation outranks the control banner because the agent is waiting,
not acting; the cancel control stays in the pill bar in that case.

```
            confirm.required
   ┌──────────────────────────────► confirm
   │                                  │ Allow
   │                                  ▼
 idle ──task.started──► working ──► working ──task.completed──► done ──6s──► idle
   ▲                      │  ▲        │
   │                      │  └────────┘ tool.started / tool.result
   │                      │ task.failed ──► error ──retry──► working
   │                      │
   │        control skill running
   │                      ▼
   │                   control ──step ends / cancel──► working / idle
   │
   │  state.changed listening       transcript.final        agent.speaking
   └───────────────────────── listening ──────────► thinking ─────────► speaking
                                   ▲                                   │
                                   └──────── interrupt / idle ◄────────┘
```

## Transitions

| From | Event | To | Notes |
|---|---|---|---|
| any | `confirm.required` | `confirm` | auto-opens the card; never auto-collapses |
| confirm | `respond(true)` | `working` | sidecar resumes the task |
| confirm | `respond(false)` / cancel | `working` → `done`/`idle` | task is denied/cancelled |
| any | running step in `CONTROL_SKILLS` | `control` | unless a confirm is pending |
| control | control step succeeds/fails | `working` / `error` | banner disappears |
| idle | `task.started` | `working` | badge updates for background tasks |
| working | `tool.started` / `tool.result` | `working` | step counter + progress |
| working | `task.completed` | `done` | holds 6s, then `idle` |
| working | `task.failed` | `error` | persists until another task starts |
| idle/working | `state.changed {state:"listening"}` | `listening` | unless confirm/error |
| listening | `transcript.partial` | `listening` | text replaces in place |
| listening | `transcript.final` | `thinking` | final text kept in the card |
| thinking | `agent.speaking` | `speaking` | TTS is fire-and-forget; window is estimated |
| speaking | `state.changed {state:"idle"}` / interrupt | `listening`/`idle` | barge-in via `POST /api/voice/interrupt` |

`CONTROL_SKILLS` (`islandState.ts`): `computer_click*`, `computer_type`,
`computer_key`, `computer_scroll`, `computer_paste`, `type_text`,
`focus_window`, `minimize_all_except`, `file_dialog_save`.

## Sizes and layout

| Size | Width | Used by |
|---|---|---|
| small | 196px | idle, done |
| medium | 404px | listening, speaking, thinking, working, error, control |
| large | 460px | confirm, expanded card |

The Electron window is resized to exactly the rendered root (padding included)
through `ResizeObserver → island:resize`. The window is re-centred on the
primary display when the size changes.

## Event contract (sidecar → island)

| Event | Payload used |
|---|---|
| `state.changed` | `state` (`idle/listening/thinking/speaking`), `muted`, `level` (0–1), `voice:"stopped"` |
| `transcript.partial` | `text` (re-transcribed every ~1s while speech is present) |
| `transcript.final` | `text`, `modality` |
| `agent.speaking` | `text` (spoken reply) |
| `tool.started` / `tool.result` | `skill`, `args`, `success`, `elapsed_ms` |
| `thinking.delta` / `thinking.summary` | not rendered inline; dashboard only |
| `answer.delta` / `answer.final` | answer snippet (truncated) |
| `confirm.required` | `tier`, `tool`, `args`, `message`, `expires_at` |
| `task.started` / `completed` / `failed` / `cancelled` | state + one-line result |

Voice stays local. `WINBROW_PARTIAL_STT=0` disables rolling partial
transcription if the extra CPU is unwanted.

## Escalation rules

- Permission prompt → open the card, keep it open until Allow/Deny.
- Confirmation expiry → show the sidecar's `expires_at` as a countdown. When it
  passes, dismiss the card and flash "Request expired". Nothing is auto-approved
  and the blocked action never runs.
- Control mode → high-contrast banner regardless of expansion; Stop is always
  visible, plus `Ctrl+Alt+Esc`.
- Done/error → one-line result with **Open**; errors also offer **Retry**.
- Text input and quick actions send a normal task and collapse back to the pill
  so progress is visible there.
- Anything longer than the card (full conversation, traces, settings, files)
  lives in the dashboard. The island only links to it.

## Non-interference guarantees

- **Click-through by default.** The main process hit-tests the real cursor
  against the island bounds every ~80 ms and toggles `setIgnoreMouseEvents`.
  Outside the pill/card, clicks go to the app underneath; inside, the island
  receives them. This cannot depend on forwarded DOM `mouseenter` events,
  which are unreliable while a window is ignoring the mouse.
- **Never steals focus.** The window is `focusable: false` and shown with
  `showInactive()`. Focus is enabled only when the user clicks the text input
  (`island:focus`) and is dropped again on blur (`island:blur`).
- **Small idle footprint.** 196×36, 84% opacity; becomes opaque on hover.
- **No overlay on fullscreen spaces.** `visibleOnFullScreen: false`.
- **Auto-collapse** after 8s without interaction, but never while a confirmation
  or control banner is active or while the composer has text.
- **Quiet motion.** Short springs, 1–1.6s loops, and `prefers-reduced-motion`
  disables the waveform/spinner/mic-ring animations.

## Pause?

There is no pause in the backend, so the island exposes **cancel** only. It does
not pretend to pause a run.

## Design review

Every state is reachable locally without the sidecar:

```
http://localhost:5173/?win=island&demo=idle
http://localhost:5173/?win=island&demo=listening
http://localhost:5173/?win=island&demo=working
http://localhost:5173/?win=island&demo=confirm
http://localhost:5173/?win=island&demo=control
http://localhost:5173/?win=island&demo=done
http://localhost:5173/?win=island&demo=error
```

Add `&open=1` to preview the expanded card. `?demo=1` (or any demo value) skips
the WebSocket and voice polling so the fixture is stable.
