---
name: open-notepad
skill: open_notepad
args: []
description: >
  Launch the Notepad text editor. Use when the user asks to open Notepad or a
  plain text editor for quick typing. Do not use it to open an existing text
  file or to create a named file on disk.
---

## When to Use

Use when the user wants the Notepad application itself, such as "open Notepad"
or "open a text editor" with no target file. It is a learned one-shot launcher
that starts the process and reports success.

## When NOT to Use

- Do not use it to open an existing file; use open-file so the file is loaded.
- Do not use it to create a named file; use create-text-file.
- Do not use it for a quick note that should be saved and opened; use
  quick-note.
- Do not use it for another editor such as VS Code; use the matching app tool or
  app-focus-or-launch.

## Step Pattern

1. Confirm the user wants Notepad with no specific file.
2. Call open-notepad.
3. Focus the new window if needed.
4. Type text with type-text or computer-type when the task requires it.
5. Verify the window exists before emitting done.

## Args Detail

This tool takes no arguments. It always starts Notepad rather than reusing an
existing window, so repeated calls may create multiple windows.

## Output Shape

Returns `success` and `output` confirming that Notepad was opened. Success means
the process started, not that the window is frontmost.

## Recovery

If Notepad does not appear, use app-focus-or-launch with `app_name="notepad"` as
a fallback. Do not repeat the same launch more than once. Follow the
self-correct rule and verify the window before claiming success.
