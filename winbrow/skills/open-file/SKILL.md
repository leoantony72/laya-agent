---
name: open-file
skill: open_file
args:
  - name: target
    type: string
    required: true
    description: File name or absolute path of the file to open.
  - name: app
    type: string
    required: false
    default: ""
    description: Optional application to open with, such as Chrome or Notepad.
description: >
  Find a file by name in the common user folders and open it in the default app
  or a named app. Use when the user wants to view or launch an existing file.
  Do not use it to create a file, open a folder, or read content silently.
---

## When to Use

Use when the goal is to open, view, edit or launch an existing file. It resolves
the file in Desktop, Documents, Downloads and other user folders, then hands it
to the associated application. It is also a safe way to make a created file
visible for verification.

## When NOT to Use

- Do not use it to create a file; use create-text-file.
- Do not use it to open a folder; use open-folder.
- Do not use it to upload a file to a website; use browser-upload-file.
- Do not use it to silently inspect file contents; use the filesystem read
  tools or run-command when the content must be examined.
- Do not pass a folder path here; the resolver expects a file.

## Step Pattern

1. If the path is unknown, call find-files first and use the exact match.
2. open-file with `target` set to the returned path or filename.
3. If the wrong app opens, retry once with the explicit `app` argument.
4. Verify the result from WINDOWS CONTEXT or the tool output (frontmost window
   or launch confirmation) before emitting done.

## Args Detail

- `target` may be a bare filename or an absolute path. A bare name is resolved
  in the common user folders; an absolute path is opened directly.
- `app` is optional. Use it only when the user names an application or the
  default association is wrong. Pass a friendly name such as Chrome, Notepad or
  Word.
- If the file does not exist, the tool reports a resolve failure; do not retry
  the same name blindly.

## Output Shape

Returns `success`, `output` (what was opened and in which app), and often the
resolved path. A launch success means the process was started, not that the
document rendered correctly.

## Recovery

If the file is not found, call find-files with the exact name and one fuzzy
variant, then clarify with the candidate folders. If the file opens in the wrong
app, retry once with `app`; otherwise ask the user. Follow the self-correct
rule and never claim the file is open without a visible window or a successful
launch message in HISTORY.
