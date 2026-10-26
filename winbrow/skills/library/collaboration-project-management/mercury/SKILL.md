---
name: mercury
skill: playbook_mercury
kind: playbook
category: collaboration-project-management
source: https://www.teamoffsite.ai/proton/docs/skill (unavailable; redirects to Oasis HQ)
license: unknown
args: []
description: >
  Coordinate agent teammates through a Mercury MCP server: threads, tasks, and
  scheduled automations. Use only when Mercury is configured.
---

# Mercury Agent Coordination

> Source note: the original Mercury skill documentation URL now redirects to the
> Oasis HQ site, whose agent-skills index is empty. This playbook reconstructs
> the workflow from the public entry description and does not claim to copy a
> current source.

## When to Use

Use when GOAL asks to message another agent, manage a shared thread, create a
task for a teammate, or schedule an automation in a Mercury workspace.

## When NOT to Use

- Do not use without a Mercury MCP URL and token in Integrations.
- Do not use for human-only chat (email, Slack, Teams).
- Do not create tasks that spend money, deploy, or contact customers without
  explicit approval.
- Do not invent tool names; inspect the MCP server's tool list first.

## Prerequisites

- Mercury MCP server URL and token in Integrations.
- The MCP tools appear as `mcp_<server>_<tool>` in OTHER TOOLS.
- If no tools appear, the server is not connected; stop and ask the user to
  configure it.

## Step Pattern

1. Call `integration_status("mercury")` if available; stop if unconfigured.
2. Read the MCP tool index and identify the exact tools for:
   - sending a message
   - creating/updating a thread
   - creating a task
   - scheduling an automation
3. Start with a read-only call: list threads, tasks, or teammates. Do not guess
   IDs.
4. Draft the message/task with: recipient, objective, context, due date, and
   acceptance criteria.
5. Confirm the recipient and content with the user before sending.
6. Send or create, then read back the thread/task to verify.
7. Report IDs, status, and any scheduled time.

## Safety Rules

- Never send a message on behalf of the user without confirmation.
- Never schedule a recurring automation without an end condition.
- Do not put credentials or customer data in a task description.
- If the server returns an unknown-tool error, stop; do not invent a fallback.

## Output Shape

A thread/task summary with IDs, status, recipients, and scheduled time.

## Recovery

- MCP server unavailable: ask the user to check the URL/token in Integrations.
- Tool schema mismatch: list the server's tools and adapt.
- Partial failure: report exactly what was created and what was not.
- No Mercury account: say the skill cannot run and offer a generic checklist.
