---
name: run-command
skill: run_command
args:
  - name: command
    type: string
    required: true
    description: Exact PowerShell command to run.
  - name: cwd
    type: string
    required: false
    default: ""
    description: Absolute working directory for the command.
  - name: timeout
    type: int
    required: false
    default: 60
    description: Seconds before the command is aborted, at most 120.
description: >
  Run a PowerShell command and return stdout, stderr and the exit code. Use to
  compile, build, test or verify code, or to run a deterministic one-shot
  command. Do not use it to start long-running servers or GUI apps.
---

## When to Use

Use for build, test and verification steps (`go build ./...`, `npm test`,
`python -m pytest`), for deterministic shell operations that no dedicated
skill covers, and for inspecting command output. It is the correct skill when
the goal requires proof that code compiles or behaves as expected.

## When NOT to Use

- Do not use it to start a server, daemon or GUI app that does not exit; the
  step will time out.
- Do not use it to search for files; use find-files.
- Do not use it to move or rename a file when move-file works.
- Do not use it to author a new reusable tool; use build-tool.
- Do not use it to repair a broken learned tool; use repair-tool.
- Do not put destructive commands in it; policy blocks them.

## Step Pattern

1. Resolve the project folder and confirm it exists.
2. run-command with the exact command and `cwd`.
3. Read stdout, stderr and `exit_code`. A non-zero exit code is a failure even
   when the output looks reassuring.
4. If the command fails, fix the code or the command once, then retry.
5. Only after a successful build/test/run may the task be marked done.

## Args Detail

- `command` is a PowerShell command or pipeline. Keep it deterministic and
  non-interactive.
- `cwd` must be an existing absolute directory. Commands run there for the
  duration of the step.
- `timeout` defaults to 60 and is clamped to 5-120 seconds. Use a longer value
  only for genuinely slow builds.
- Destructive operations, privilege escalation and execution-policy changes are
  rejected by the policy layer before execution.
- Output is truncated at the tool limit; a truncated tail may hide the final
  error, so prefer focused commands.

## Output Shape

Returns `success`, `output` (stdout plus stderr), `exit_code`, `command`,
`cwd` and `elapsed_ms`. Success is true only when the exit code is zero.

## Recovery

On failure, read stderr first and correct the command or the code. Retry once
with the fix; do not repeat the identical command. If a learned tool's script
is broken, call repair-tool. If no listed skill can perform the operation,
call build-tool for one small atomic tool. Follow the self-correct rule and
never report a task as verified without a zero exit code.
