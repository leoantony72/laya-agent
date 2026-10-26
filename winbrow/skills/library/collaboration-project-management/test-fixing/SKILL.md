---
name: test-fixing
skill: playbook_test_fixing
kind: playbook
category: collaboration-project-management
source: https://github.com/mhattingpete/claude-skills-marketplace/tree/main/engineering-workflow-plugin/skills/test-fixing
license: see upstream repository
args: []
description: >
  Diagnose and fix failing tests one group at a time, with verification after
  every change. Use when a test suite is red.
---

# Test Fixing

## When to Use

Use when GOAL asks to fix failing tests, make a suite pass, or diagnose a test
failure after a change.

## When NOT to Use

- Do not use to weaken or delete a test just to make it pass.
- Do not use for a production incident; use `root-cause-tracing` first.
- Do not rewrite the test framework or unrelated tests.
- Do not skip a failing test without documenting why.

## Step Pattern

1. Run the narrowest failing suite with `run_command` and capture the full
   output, exit code, and command.
2. Group failures by root symptom, not by file:
   - same assertion / same missing fixture
   - same import or environment error
   - flaky/timing
   - genuinely independent
3. Pick the group with the highest signal (usually the first infrastructure
   error, because it hides the rest).
4. Reproduce one test in isolation. Read the test and the code it exercises.
5. State the expected behavior and the smallest fix. Change one thing.
6. Re-run the single test, then the group, then the suite.
7. If a test itself is wrong, fix the test only when the intended behavior is
   clear; otherwise ask.
8. Record: failure, cause, change, command, result.

## Rules

- Never mark a test `skip`/`xfail` to get green unless the user approves.
- Never loosen an assertion without explaining what behavior is now expected.
- Do not edit production code and tests in the same step unless the test is the
  spec and the change is trivial.
- If a test is flaky, run it 3–5 times and report the failure rate.
- Keep the final diff limited to the failing area.

## Output Shape

A failure table (test, symptom, cause, fix, verification) and the final suite
result. Include exact commands.

## Recovery

- Cannot reproduce: report the environment difference and ask for CI logs.
- Fix breaks another test: revert and re-trace.
- Missing dependency/service: report the blocker and the install/start command.
- Test suite is too slow: run the affected subset first, then the full suite.
