---
name: move-file
skill: mcp_filesystem_move_file
args:
  - name: source
    type: string
    required: true
    description: Absolute path of the file or directory to move.
  - name: destination
    type: string
    required: true
    description: Absolute destination path, including the new file name.
description: >
  Move or rename a file or directory through the MCP filesystem server. Use
  after find-files returns an exact source path and the destination folder
  exists. Do not use it to copy content or to move into a folder that has not
  been created.
---

## When to Use

Use for moving or renaming an existing file or directory once both absolute
paths are known. It is the preferred file-operation skill because it stays
inside the filesystem server's allowed directories and reports a clear result.

## When NOT to Use

- Do not use it to copy a file and keep the original; use run-command with
  Copy-Item or the filesystem copy operation when one is registered.
- Do not use it when the destination folder does not exist; create it first
  with mcp_filesystem_create_directory.
- Do not use it with a bare filename; both paths must be absolute.
- Do not use it to download or upload; use download-file or
  browser-upload-file.
- Do not overwrite an existing destination; the server fails instead of
  replacing it. Choose a new name or clarify.

## Step Pattern

1. find-files resolves the exact `source` path.
2. If the destination folder is new, call mcp_filesystem_create_directory.
3. move-file with the full `source` and `destination` paths.
4. Verify with mcp_filesystem_list_directory on the destination folder, or
   mcp_filesystem_get_file_info on the new path.
5. If the move is part of a larger task, continue with the next step using the
   destination path exactly as returned.

## Args Detail

- `source` must be an existing absolute path inside an allowed directory.
- `destination` must be an absolute path including the file name. The parent
  folder must already exist.
- If the destination exists, the operation fails without overwriting. Pick a
  different filename or ask the user.
- Cross-volume moves and renames in the same folder are both supported.
- If the MCP filesystem server is unavailable, fall back to a single verified
  run-command with Move-Item, never to an unverified repeat.

## Output Shape

Returns `success`, `output` (the move confirmation), and the MCP server's result
text. A success means the server completed the operation, not that the file is
readable by another application yet.

## Recovery

If the source is missing, re-run find-files and verify the path. If the
destination folder is missing, create it and retry once. If the destination
exists, choose a new name or clarify. If the server is unavailable, use one
run-command fallback and verify with list-directory. Follow the self-correct
rule; never repeat the identical failing move.
