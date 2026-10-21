---
name: recall-memory
skill: recall_memory
args:
  - name: query
    type: string
    required: true
    description: What to search for in past activity or saved facts.
description: >
  Search past activity, saved facts and episodic history. Use when the user asks
  what they did before, asks you to recall a preference, or references an
  earlier task. Do not use it to act on the current desktop or to search files
  on disk.
---

## When to Use

Use for questions about the past: "what did I open yesterday", "where did I save
that file", "what is my exports folder", "did I already do this". It returns
matching labels and timestamps so the answer can be grounded in real history.

## When NOT to Use

- Do not use it to find a file that exists on disk; use find-files.
- Do not use it to inspect the current page or desktop; use the observation
  skills.
- Do not use it to save a new fact; use remember-fact.
- Do not use it when CONVERSATION already contains the answer.

## Step Pattern

1. Identify the entity or phrase to search for.
2. Call recall-memory with a short, specific query.
3. Read the returned hits and timestamps.
4. If a hit names a path or action, use that exact value in the next step.
5. If nothing matches, say so honestly and ask for the missing detail.

## Args Detail

- `query` should be specific: an app name, a filename, a folder or a phrase.
- A broad query may return many hits; prefer the most recent relevant one and
  mention its timestamp.
- Empty output means no stored match, not that the event never happened.

## Output Shape

Returns `success` and `output` with matching labels and timestamps, or the text
"No matching past activity found."

## Recovery

If no match is found, try one shorter query, then ask the user instead of
guessing. Never invent a memory. Follow the self-correct rule and do not
repeat the same empty query.
