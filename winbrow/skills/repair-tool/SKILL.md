---
name: repair-tool
skill: repair_tool
args:
  - name: skill_name
    type: string
    required: true
    description: Name of the broken tool or skill.
  - name: failure
    type: string
    required: true
    description: What went wrong, including the error text or wrong result.
  - name: task
    type: string
    required: false
    default: ""
    description: Optional restatement of what the tool should accomplish.
description: >
  Diagnose a tool that just failed, rewrite its script, register the fix and
  verify it with one run. Use after a learned or scripted tool returns an error
  or an empty wrong result. Do not use it for tools implemented in Python.
---

## When to Use

Use when a scripted or learned tool fails in HISTORY and the failure is in the
tool itself, not in its arguments: an exception, a missing variable, a wrong
path template or an empty result. It rewrites the script, registers the fix and
runs it once to verify.

## When NOT to Use

- Do not use it before reading the failure; a bad argument should be corrected
  directly.
- Do not use it for Python-backed skills such as browser-read-page or
  screen-ocr; their code is not a PowerShell script.
- Do not use it when the capability is missing entirely; use build-tool.
- Do not use it to redesign or expand a working tool.
- Do not call it twice for the same failure without new information.

## Step Pattern

1. Read the failed step's output and identify the defect.
2. Call repair-tool with the exact tool name and the failure text.
3. The fixer rewrites the script and registers it.
4. The tool is run once to verify; read the verification output.
5. Retry the original operation with the repaired tool or its saved variant.
6. If the repair fails, emit clarify instead of another blind repair.

## Args Detail

- `skill_name` must match the registered tool name exactly.
- `failure` should quote the error or describe the wrong result precisely.
- `task` is optional but helps when the original intent is no longer clear.
- A seed tool is saved as a `<name>_fixed` sibling; use that name after repair.
- Repaired scripts still pass the safety policy; destructive fixes are rejected.

## Output Shape

Returns `success`, `output` (the repaired name, saved name and verification
result) and may include the new script. A successful repair means the fix ran
once successfully; it does not complete the original goal by itself.

## Recovery

If the repair is rejected or the verification fails, do not loop. Read the
failure, try the original tool with corrected arguments, or emit clarify with
what was attempted. Follow the self-correct rule and never report a repair as
successful without a successful verification run.
