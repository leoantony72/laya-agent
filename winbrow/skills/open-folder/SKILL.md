---
name: open-folder
skill: open_folder
args:
  - name: folder
    type: string
    required: false
    default: downloads
    description: Common folder name or an absolute folder path.
description: >
  Open a folder in File Explorer, either a common user folder or an absolute
  path. Use when the user wants to see or browse a folder. Do not use it for
  files, to create a folder, or to move content.
---

## When to Use

Use when the goal is to show a directory in Explorer: Desktop, Documents,
Downloads, Pictures, Music, Videos or any named folder. It is also useful after
a move or create operation to make the result visible for verification.

## When NOT to Use

- Do not use it to open a file; use open-file.
- Do not use it to create a directory; use the filesystem create-directory tool
  or run-command.
- Do not use it to move or copy files; use move-file or run-command.
- Do not pass a file path; Explorer may select the file, but the intended
  action is unclear. Confirm the target with find-files first.

## Step Pattern

1. Resolve the folder name or path. If the folder is newly created, complete
   the create step first.
2. open-folder with the resolved name or path.
3. Verify from WINDOWS CONTEXT or the tool output that Explorer is frontmost.
4. If the user wants contents listed instead of a window, use the filesystem
   list-directory tool instead.

## Args Detail

- `folder` accepts common names (Desktop, Documents, Downloads, Pictures,
  Music, Videos, Home) or an absolute path such as `C:\Users\Me\Projects`.
- A relative or misspelled name may resolve to a folder with a similar name or
  fail; use the exact path when in doubt.
- The default `downloads` is only a fallback; always pass the user's folder.

## Output Shape

Returns `success`, `output` (the folder that was opened), and usually the
resolved path. A launch success means Explorer was invoked, not that the window
has finished rendering.

## Recovery

If the folder is not found, use find-files or the filesystem list-directory
tool to resolve it, then retry once. If the path is a file, switch to open-file.
If two folders share a name, emit clarify with the candidate paths. Follow the
self-correct rule before retrying.
