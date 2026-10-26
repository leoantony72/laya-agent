---
name: git-pushing
skill: playbook_git_pushing
kind: playbook
category: collaboration-project-management
source: https://github.com/mhattingpete/claude-skills-marketplace/tree/main/engineering-workflow-plugin/skills/git-pushing
license: see upstream repository
args: []
description: >
  Stage, commit, and push changes with a clear conventional commit message. Use
  when the user asks to commit or push their work.
---

# Git Pushing

## When to Use

Use when GOAL asks to commit, push, save work to the remote, or prepare a
branch for review.

## When NOT to Use

- Do not push to a protected branch or a shared remote without explicit
  confirmation.
- Do not commit secrets, credentials, large binaries, or unrelated changes.
- Do not force-push unless the user explicitly asks and understands the risk.
- Do not rewrite history or rebase a shared branch.

## Prerequisites

- The `git` MCP server (`mcp_git_git_*`) or `run_command` with `git`.
- The repository path. Ask if it is not the current workspace.

## Step Pattern

1. `mcp_git_git_status` and `mcp_git_git_diff` to see exactly what changed.
2. Read the diff for secrets and unrelated edits. Stop and ask if anything is
   suspicious.
3. Stage the intended files only: `mcp_git_git_add` with explicit paths. Do not
   use `git add -A` if unrelated changes exist.
4. Write a Conventional Commit message:
   `<type>(<scope>): <imperative summary>` with a short body for why.
5. `mcp_git_git_commit`.
6. Show the commit hash and a one-line summary.
7. Ask for explicit confirmation before `mcp_git_git_push`. State the branch
   and remote.
8. Push, then verify with `mcp_git_git_status` or `git log` that the branch is
   up to date.

## Safety Rules

- Never commit `.env`, tokens, private keys, customer data, or dumps.
- Never force-push to `main`/`master` or a branch you did not create.
- Never amend a commit that was already pushed unless the user asks.
- If hooks fail, report the hook output; do not bypass with `--no-verify`
  without permission.
- If the remote rejects the push, fetch/inspect first; do not force.

## Output Shape

Files staged, commit hash and message, branch/remote, and push result.

## Recovery

- Nothing to commit: report clean status.
- Merge conflict: stop and ask which resolution the user wants.
- Auth failure: report the remote and ask the user to re-authenticate.
- Hook failure: fix the reported issue or ask; never silently skip hooks.
