---
name: create-folder-papers
skill: create_folder_papers
args: []
description: >
  Create a folder named "papers" on the user's Desktop, reusing it if it already
  exists. Use when the request names exactly that folder on the Desktop. Do not
  use it for a differently named folder or another location.
---

## When to Use

Use when the user asks to create or ensure a Desktop folder named "papers".
The learned script is idempotent: it creates the directory if missing and
returns the resulting path.

## When NOT to Use

- Do not use it for a folder with a different name; use the filesystem
  create-directory tool or run-command.
- Do not use it for a non-Desktop location.
- Do not use it to move files into the folder; follow with move-file.
- Do not use it to open the folder; use open-folder afterward if needed.

## Step Pattern

1. Confirm the requested folder is exactly "papers" on the Desktop.
2. Call create-folder-papers.
3. Use the returned path for any follow-up move or upload.
4. Verify with open-folder or the filesystem list-directory tool when the task
   requires visual confirmation.

## Args Detail

This tool takes no arguments and is bound to the Desktop and the name
"papers". It is idempotent, so calling it when the folder exists succeeds
without changing anything.

## Output Shape

Returns `success` and `output` with the created or existing folder path. The
path is stable and can be used verbatim in subsequent steps.

## Recovery

If folder creation fails, fall back to mcp_filesystem_create_directory with the
full Desktop path. If the user actually wanted a different name or location,
switch to the generic create-directory tool instead of this learned skill.
Follow the self-correct rule.
