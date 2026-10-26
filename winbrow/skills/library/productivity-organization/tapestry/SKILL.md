---
name: tapestry
skill: playbook_tapestry
kind: playbook
category: productivity-organization
source: https://github.com/michalparkola/tapestry-skills-for-claude-code
license: see upstream repository
args: []
description: >
  Interlink related notes and documents into a knowledge network with summaries
  and cross-references. Use when the user has a pile of related material.
---

# Tapestry: Knowledge Networks

> Source note: the upstream README link for "tapestry" is stale and the
> repository has no `tapestry/SKILL.md`. This playbook reconstructs the
> described workflow; it does not copy a missing source file.

## When to Use

Use when GOAL asks to connect, interlink, map, or summarize a set of related
documents, notes, transcripts, or research files.

## When NOT to Use

- Do not use for a single document; summarize it directly.
- Do not use for a folder of unrelated files with no shared topic.
- Do not invent links that are not supported by the text.
- Do not replace the source files; write a new index.

## Step Pattern

1. List the sources (paths or URLs) and confirm the shared question.
2. Read each with `read_document` or `browser_read_full`. Extract:
   - core claim
   - key entities (people, orgs, concepts)
   - evidence or data
   - open questions
   - relation to the other sources
3. Build a concept table: concept → source → what it says.
4. Classify relationships:
   - supports
   - contradicts
   - extends
   - duplicates
   - prerequisite
5. Write a network index:
   - overview
   - concepts with links to sources
   - tensions/contradictions
   - reading order for a newcomer
   - gaps and next sources
6. Use Markdown links between local files where possible.
7. Save `index.md` in the shared folder and read it back.

## Output Shape

A `index.md` knowledge map, a concept table, and a short reading path.

## Recovery

- Sources are too different: narrow the shared question or split into clusters.
- Duplicate content: merge into one concept and cite both sources.
- Contradiction: present both and say which evidence is stronger.
- Missing source: list it as a gap; do not fill it from memory.
