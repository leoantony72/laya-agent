---
name: deep-research
skill: playbook_deep_research
kind: playbook
category: data-analysis
source: https://github.com/sanjay3290/ai-skills/tree/main/skills/deep-research
license: Apache-2.0
args: []
description: >
  Run a cited, multi-source research pass with Gemini Search grounding. Use for
  market, competitor, literature, or due-diligence briefs.
---

# Deep Research

## When to Use

Use when GOAL asks for a researched brief with sources: market analysis,
competitive landscape, literature review, technical due diligence, or a
decision memo that needs current facts.

## When NOT to Use

- Do not use for a one-line fact or a simple web lookup; use `browser_read_full`
  or the `fetch` MCP tool.
- Do not use for private documents only the user has; read those directly.
- Do not use when the user forbids external API calls.
- Do not present a single-source claim as consensus.

## Prerequisites

- A Gemini API key in the Integrations panel or `GEMINI_API_KEY`.
- `run_skill_script` with `skill="deep-research"` and
  `script="research_gemini.py"`.
- `httpx` is installed. If the key is missing, the script returns an actionable
  error; emit `clarify` with the setup path instead of retrying.

## Step Pattern

1. If available, call `integration_status("gemini")`. If it is not configured,
   ask the user to open Integrations and add the key; stop.
2. Clarify the research question and the required output structure if GOAL is
   vague. Do not invent a scope.
3. Call `run_skill_script`:
   `{"operation":"research","query":"...","format":"1. Executive summary\n2. Comparison table\n3. Recommendation","model":"gemini-2.5-flash"}`.
4. Read the returned answer and source list. Check that every major claim has a
   source; mark unsupported claims.
5. If the user asked for a file, write the report once with
   `create_text_file` or `mcp_filesystem_write_file`, then read it back.
6. Report the model used, the source count, and the limits of the search.

## Spec Detail

- `query` is required.
- `format` is optional; it is appended to the prompt as required structure.
- `model` defaults to `gemini-2.5-flash`.
- `timeout` defaults to 120 seconds.
- `max_output_tokens` defaults to 4096.

## Output Shape

Returns the report text plus `data.sources` as `{title, url}` objects.

## Recovery

- HTTP 401/403: the key is invalid; ask the user to re-enter it.
- HTTP 429: quota exhausted; report the limit and suggest retrying later.
- No sources returned: say the answer is ungrounded and do not present it as
  researched fact.
- Timeout: narrow the query and retry once.
