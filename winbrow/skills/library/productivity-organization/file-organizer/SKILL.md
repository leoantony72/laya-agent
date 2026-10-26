---
name: file-organizer
skill: playbook_file_organizer
kind: playbook
category: productivity-organization
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Find duplicate files and propose or apply a folder organization plan. Use for
  messy Downloads, Desktop, and project folders.
---

# File Organizer

## When to Use

Use when GOAL asks to clean up, organize, de-duplicate, or restructure a folder,
or to find files taking up space.

## When NOT to Use

- Do not move files outside the folder the user named.
- Do not delete anything; this skill moves or copies only.
- Do not organize system folders, Program Files, or another user's profile.
- Do not apply a plan without showing it first unless the user explicitly said
  "just do it".

## Prerequisites

- `run_skill_script` with `skill="file-organizer"` and
  `script="find_duplicates.py"`.
- Existing filesystem tools for simple moves:
  `mcp_filesystem_move_file`.

## Step Pattern

1. Confirm the root folder and whether subfolders are included.
2. Find duplicates:
   `{"operation":"duplicates","path":"C:\\Users\\me\\Downloads","min_size":1024}`.
   Show groups and recoverable bytes. Never delete a duplicate automatically.
3. Build a plan:
   `{"operation":"plan","path":"...","target":"C:\\Users\\me\\Downloads\\Organized"}`.
   Review the category mapping with the user.
4. Apply with a dry run first:
   `{"operation":"apply","moves":[...],"dry_run":true}`.
   Then, after explicit approval, `"dry_run":false`.
5. Verify a sample of destinations and report counts. Do not claim every file
   moved without checking.

## Safety Rules

- Never overwrite an existing destination unless the user explicitly approves.
- Keep a list of every source/destination pair in the result.
- Skip symlinks and hidden system files.
- If the folder has more than 2,000 files, narrow the scope first.
- Leave a short `organize-plan.md` in the target folder when applying.

## Output Shape

`duplicates` returns groups and recoverable bytes. `plan` returns a moves list.
`apply` returns the completed (or dry-run) moves.

## Recovery

- Permission denied: report the exact path and stop.
- Name collision: choose a suffixed name or ask.
- User changes their mind after a dry run: do nothing until they approve.
- Duplicate hash fails on a locked file: skip it and list it.
