---
name: delegate-task
skill: delegate_task
kind: tool
args:
  - name: agent
    type: string
    required: false
    default: researcher
    description: "Profile: researcher, file-analyst, writer, code-reviewer, verifier, or general."
  - name: task
    type: string
    required: true
    description: One focused task for the subagent.
  - name: context
    type: string
    required: false
    default: ""
    description: Facts, paths, URLs, or constraints the subagent needs.
  - name: max_steps
    type: int
    required: false
    default: 0
    description: Optional action budget; 0 uses the profile default.
  - name: tasks
    type: string
    required: false
    default: ""
    description: Optional JSON array of {agent, task, context, max_steps} objects.
  - name: parallel
    type: bool
    required: false
    default: false
    description: Run independent tasks concurrently when every profile is parallel-safe.
description: >
  Delegate one or more focused, independent tasks to isolated subagents. Use
  automatically when work is independent and benefits from a focused worker;
  do not delegate simple one-tool actions or dependent next steps.
---

# Delegating to Subagents

## When to Use

Use when GOAL contains **independent** work that benefits from a focused
worker:

- research several competitors, papers, pages, or sources
- read and compare a set of local files
- draft a document from supplied facts
- review a diff or run a focused verification
- independently verify a claim or artifact
- process a large reading job that would flood the main context

The main planner may delegate automatically when these conditions apply. The
user does not have to say “use a subagent”; they can also ask explicitly.

## When NOT to Use

- Do not delegate a simple one-tool action.
- Do not delegate a task that depends on the next action in the main loop.
- Do not delegate the same task twice; use one call with `tasks`.
- Do not delegate destructive work, pushes, messages, or payments unless the
  user explicitly approved that action. The child still hits the permission
  gate, but the delegation itself is not consent.
- Do not use `general` when a specialist profile fits.
- Prefer doing the work yourself when it is short, linear, or needs full
  conversation context.

## Profiles

| Profile | Best for | Safety |
|---|---|---|
| `researcher` | Web/local research with citations | read-only; browser serial |
| `file-analyst` | Read, compare, summarize local files | read-only; parallel-safe |
| `writer` | Create a requested text/DOCX/PDF/PPTX/XLSX/EPUB artifact | writes; sequential |
| `code-reviewer` | Read diffs, run focused checks, review code | no commit/push; sequential |
| `verifier` | Independently PASS/FAIL/UNVERIFIED a claim | read-only checks; sequential |
| `general` | A task that fits no specialist profile | broad; sequential |

## Step Pattern

1. Write one self-contained task per subagent. Include the exact path, URL,
   output format, and what counts as done.
2. Put shared facts in `context`; do not assume the child can see HISTORY.
3. Call `delegate_task`:
   - single:
     `{"agent":"researcher","task":"Compare the pricing pages of A, B, and C. Return a table with source URLs.","context":"The user is deciding between them for a 20-person team."}`
   - batch:
     `{"tasks":[{"agent":"researcher","task":"..."},{"agent":"file-analyst","task":"..."}],"parallel":true}`
4. Read the returned `subagents` list. Each entry has `status`, `summary`,
   `steps`, and `elapsed_ms`.
5. If a subagent is `blocked`, surface its question to the user (do not guess).
6. If it `failed`, decide whether to retry with a narrower task or do it
   yourself. Do not silently drop the failed part.
7. Use the subagent's evidence in the main task; verify any file/path before
   reporting it to the user.

## Parallelism

- `file-analyst` is the only profile marked parallel-safe by default.
- Browser profiles share the agent's single browser tab, so they run
  sequentially even if `parallel=true`.
- Write profiles run sequentially to avoid file races.
- At most four tasks per call; at most three run concurrently.

## Output Shape

The tool returns `success`, `output`, `subagents`, `count`, and
`parallel_used`. Each subagent result contains `status` (`done`, `blocked`, or
`failed`), `summary`, `steps`, and `elapsed_ms`.

## Recovery

- Unknown profile: list the valid profiles and retry.
- Child blocked: ask the user the child's question.
- Child failed on a missing file/credential: fix the input and retry once with
  a narrower task.
- Child returns no evidence: treat it as unverified; do not present it as fact.
- Subagents disabled: do the work in the main loop.
