---
name: find-files
skill: find_files
args:
  - name: search_term
    type: string
    required: false
    default: "*"
    description: File name or name fragment to search for in common folders.
description: >
  Search Desktop, Documents, Downloads and other common user folders for files
  matching a name or pattern. Use first for any file lookup, including fuzzy
  variants of a spoken name. Do not use for full-drive scans, file-content
  search, or when a matching path is already known.
---

## When to Use

Use at the start of any task that needs an existing file path: opening,
moving, uploading, copying, renaming or inspecting a file. It is fast because
it scopes to the common user folders instead of scanning a drive.

## When NOT to Use

- Do not use it when CONVERSATION or HISTORY already contains the exact path.
- Do not use it to search inside file contents; use run-command with a content
  search only when no dedicated skill exists.
- Do not use it for a full-drive scan; that is slow and usually times out.
- Do not use run-command to search for files; this skill exists for that.
- Do not search the same term more than twice before asking the user.

## Step Pattern

Follow this escalation and stop as soon as the file is found:

1. If a matching path is already in CONVERSATION or HISTORY, use it directly.
2. Otherwise search the exact name the user gave.
3. If nothing is found, search a fuzzy variant: drop spaces, add spaces, use
   underscores, use lowercase (for example "LDMseminar" becomes "LDM seminar",
   "ldm_seminar", "LDM_seminar").
4. If still nothing, emit clarify with the folders searched and ask for the
   location or full path.
5. Once a path is returned, pass that exact path to open-file, move-file,
   browser-upload-file or create-text-file.

## Args Detail

- `search_term` is the exact filename or a fragment. Wildcards are accepted;
  the default `*` lists everything, which is rarely useful.
- Results may contain multiple matches. If more than one file is plausible,
  list the candidates and emit clarify instead of guessing.
- A not-found result is authoritative for the searched folders, not for the
  whole disk.

## Output Shape

Returns `success`, `output` (the matching paths, one per line) and may include a
`files` list. Empty output with success means no match in the searched folders.

## Recovery

After an empty result, try one fuzzy variant, then clarify. Never repeat the
same search a third time. If a result is ambiguous, ask which file the user
means and retain the chosen path in HISTORY for the next step. Follow the
self-correct rule and do not escalate to a drive scan.
