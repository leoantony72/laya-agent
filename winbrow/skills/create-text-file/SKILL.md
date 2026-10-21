---
name: create-text-file
skill: create_text_file
args:
  - name: filename
    type: string
    required: true
    description: File name including the extension, e.g. notes.txt.
  - name: content
    type: string
    required: true
    description: Exact text to write into the file.
  - name: folder
    type: string
    required: false
    default: Desktop
    description: Known user folder (Desktop, Documents, Downloads, Pictures, Videos).
description: >
  Create a named text file with exact content in a known user folder. Use for
  notes, code snippets and small documents the user asks you to save. Do not use
  it for files inside a new subfolder or for a full absolute path.
---

## When to Use

Use when the user asks for a named text file directly in Desktop, Documents,
Downloads, Pictures or Videos and the content is already known or produced by
the current task. It is the correct skill for `notes.txt`, `chicken.go` or a
small report in a common folder.

## When NOT to Use

- Do not use it for a quick note that should open in Notepad; use quick-note.
- Do not use it for a file inside a specific subfolder or a full path such as
  `Desktop/chicken-go/chicken.go`; use mcp_filesystem_write_file with the
  complete path.
- Do not use it to append to or edit an existing file; use the filesystem edit
  or write tool.
- Do not use it for binary files, downloads or images.
- Do not use it when the user wants a folder created; use the filesystem
  create-directory tool or run-command.

## Step Pattern

1. Resolve the target folder and filename from GOAL and HISTORY.
2. If the file needs a full path or a nested folder, switch to
   mcp_filesystem_write_file and use the complete path.
3. Otherwise call create-text-file with `filename`, `content` and `folder`.
4. Read the file back with open-file or the filesystem read tool when the task
   requires verification.
5. For code, follow with run-command to build or test before declaring done.

## Args Detail

- `filename` must include an extension; a bare name may be rejected or saved
  without one.
- `content` is written verbatim. Preserve newlines, quotes and code exactly;
  do not add commentary.
- `folder` is a known user folder name, not a path. Use Desktop when the user
  says "on my desktop" or gives no folder.
- If create-text-file fails or times out, do not repeat it. Retry once with
  mcp_filesystem_write_file using the full Desktop path.

## Output Shape

Returns `success`, `output` (the created path) and the file metadata. Success
means the file was written; if the task requires the program to run, that is a
separate verification step.

## Recovery

If the write fails, check the folder name and filename, then retry once with
the full-path filesystem write tool. If the file already exists and the user
did not ask to overwrite, clarify or choose a new name. Never claim the file
exists without a successful result or a read-back. Follow the self-correct
rule.
