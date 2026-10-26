---
name: n8n-workflows
skill: playbook_n8n_workflows
kind: playbook
category: productivity-organization
source: https://github.com/haunchen/n8n-skills
license: see upstream repository
args: []
description: >
  Design, inspect, and validate n8n workflows. Use when the user works with an
  n8n instance, nodes, or workflow automation.
---

# n8n Workflows

## When to Use

Use when GOAL asks to design an n8n workflow, choose nodes, debug a run, or
connect n8n to another service.

## When NOT to Use

- Do not use without an n8n instance URL and API key, or the n8n MCP server.
- Do not activate or publish a workflow that sends external messages or moves
  money without explicit confirmation.
- Do not guess node parameter names; fetch the node schema.
- Do not assume a workflow ran successfully because it saved.

## Prerequisites

- n8n base URL and API key in the Integrations panel, or an n8n MCP server.
- Browser tools for the n8n UI if the API is not exposed.
- The community `n8n-skills` pack is a large node reference; this converted
  skill does not bundle the 545-node catalog. Use the n8n UI or API to inspect
  the exact node installed on the instance.

## Step Pattern

1. If available, call `integration_status("n8n")`; if unconfigured, ask the
   user to add the base URL and API key in Integrations and stop.
2. Confirm the trigger (webhook, schedule, manual), the input, and the desired
   output.
3. List the steps as nodes: trigger → fetch → transform → branch → action →
   error handling.
4. For each node, record the exact type, version, credentials, and required
   parameters. If a node schema is available, use it; otherwise inspect the
   node in the UI.
5. Build the workflow in a draft state. Use `Set`/`Code` nodes for small
   transformations; avoid a Code node for something a native node does.
6. Validate: required fields, connection types, error branches, and empty-input
   behavior.
7. Test with a pinned sample or a manual execution. Read the execution log.
8. Only then ask whether to activate it.
9. Save notes with: trigger, nodes, credentials used, test evidence, rollback.

## Safety Rules

- Never put credentials in a workflow JSON or a chat message.
- Confirm before any node that sends email, posts to Slack, charges a card, or
  deletes data.
- Add error handling to every external call.
- Do not loop a failing workflow; inspect the execution once, fix, retest.

## Output Shape

A workflow plan, node-by-node parameter table, validation checklist, and test
evidence. If the API is available, return the workflow ID and execution status.

## Recovery

- 401/403: re-enter the API key in Integrations.
- Node schema mismatch: use the version installed on the instance, not the
  latest docs.
- Workflow runs but output is empty: inspect the pinned input and the first
  node's output.
- Cannot access the API: use the browser UI and report the limitation.
