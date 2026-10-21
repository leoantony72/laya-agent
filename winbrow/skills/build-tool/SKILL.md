---
name: build-tool
skill: build_tool
args:
  - name: task
    type: string
    required: true
    description: Plain-language spec of the one atomic tool to build.
description: >
  Author, verify and persist a new small PowerShell tool for a request that no
  listed skill can perform. Use only when the capability is genuinely missing.
  Do not use it for multi-step goals or for operations an existing skill covers.
---

## When to Use

Use when GOAL needs a capability that no skill or tool in the catalog provides,
and the capability is small and atomic: a single system query, a one-shot
transformation or a narrow automation. The tool is run once to verify and is
saved for reuse.

## When NOT to Use

- Do not use it to move, copy, rename, list or create files; the filesystem
  tools already cover those.
- Do not use it to compile, build, test or run an existing command; use
  run-command.
- Do not use it to repair a broken learned tool; use repair-tool.
- Do not cram a whole multi-step goal into one build task. Build one tool, then
  use it on the original request in a follow-up turn.
- Do not build a tool that duplicates an existing skill's capability.

## Step Pattern

1. Confirm that no listed skill or tool can perform the operation.
2. Describe exactly one tool: inputs, behavior and output.
3. Call build-tool with that spec.
4. Read the build result: the new tool name is registered for the next turn.
5. Use the new tool on the original request in the follow-up planning turn.
6. Verify its output before declaring done.

## Args Detail

- `task` is a plain-language specification of ONE tool. Include the expected
  input type, the exact operation and the returned message.
- Do not include several operations or conditional flows; split them into
  separate atomic tools only if truly necessary.
- The generated script passes through the safety policy; destructive commands
  are rejected.

## Output Shape

Returns `success`, `output` (including the registered tool name) and may include
the generated script. A successful build means the tool was created and usually
run once; it does not mean the original goal is complete.

## Recovery

If the build fails because no model provider is configured, report that
honestly. If the generated tool errors, call repair-tool with its name and the
failure text. If the capability actually exists, switch to the existing skill.
Follow the self-correct rule and never pretend a missing capability worked.
