---
name: root-cause-tracing
skill: playbook_root_cause_tracing
kind: playbook
category: data-analysis
source: https://github.com/obra/superpowers/tree/main/skills/systematic-debugging
license: see upstream repository
args: []
description: >
  Trace a bug or failure back to its first cause before proposing a fix. Use
  when an error appears deep in a run or a fix keeps failing.
---

# Root Cause Tracing

## When to Use

Use when a command, test, script, or workflow fails and the visible error is
not the cause. Use after one quick fix attempt has failed, or when the same
symptom keeps returning.

## When NOT to Use

- Do not use for a typo or a missing file with an obvious message.
- Do not use to guess at a fix before reproducing the failure.
- Do not use to rewrite unrelated code.
- Do not use when the user only wants the error explained; answer directly.

## Step Pattern

1. **Capture the exact failure** — run the smallest failing command with
   `run_command`, and keep the full error, exit code, and working directory.
2. **Reproduce deterministically** — find the minimal input and command that
   fails every time. If it is intermittent, record the conditions.
3. **Trace backwards** — follow the call chain from the error to the first
   point where state became wrong. Read the source at each hop; do not assume.
4. **Instrument** — add one temporary log or assertion at the suspected
   boundary. Re-run. Remove or keep the instrumentation deliberately.
5. **State one hypothesis** — "X is empty because Y ran before Z". Predict what
   the next observation will show before testing it.
6. **Test the hypothesis** — change one variable at a time. If the prediction
   fails, discard the hypothesis; do not patch around it.
7. **Fix the first cause** — make the smallest change at the earliest wrong
   point. Add a regression test or a repeatable check.
8. **Verify** — run the failing case and the nearest healthy case. Report both.
9. **Explain** — symptom, root cause, fix, evidence, and remaining risk.

## Evidence Rules

- Distinguish observed facts from inferences.
- Quote the exact error line and the file/line that produced it.
- Do not claim a fix works because the code looks right; run it.
- If the cause cannot be found, report the boundary reached and the next
  experiment. Do not invent a cause.

## Output Shape

A short incident note:
`Symptom → Reproduction → First wrong state → Root cause → Fix → Verification`.

## Recovery

- Cannot reproduce: ask for the exact command, input, and time.
- Fix makes another test fail: revert and re-trace; do not stack fixes.
- Environment-specific: record OS, Python version, and paths.
