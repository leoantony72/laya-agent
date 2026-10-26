---
name: content-research-writer
skill: playbook_content_research_writer
kind: playbook
category: communication-writing
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Research, outline, draft, and revise an article with citations and section
  feedback. Use for blog posts, newsletters, and thought-leadership pieces.
---

# Content Research Writer

## When to Use

Use when the user wants a researched article, newsletter, tutorial, case study,
or technical post with a clear outline, citations, and revision feedback.

## When NOT to Use

- Do not use for a short social post; use `twitter-optimizer` or write directly.
- Do not use for fiction, poetry, or purely personal writing.
- Do not use when the user only wants editing of an existing draft; edit it
  directly with targeted changes.
- Do not invent sources, quotes, or statistics.

## Step Pattern

1. **Collect the brief** — audience, goal, angle, length, voice, deadline, and
   any must-include sources. Ask only for what is missing.
2. **Research** — use `browser_navigate` + `browser_read_full`, `fetch`, or
   `deep-research` for a heavier brief. Keep a source table as you go.
3. **Outline** — propose 4–8 sections with one sentence each. Get approval
   before drafting.
4. **Draft in sections** — for each section: claim, evidence, example, and
   transition. Preserve the user's voice; do not flatten it into corporate
   prose.
5. **Add citations** — link the exact source next to the claim. Mark anything
   unverified.
6. **Feedback pass** — for each section, give one strength, one weakness, and
   one concrete revision. Apply the revision or ask which version to keep.
7. **Polish** — strengthen the hook, remove throat-clearing, vary sentence
   length, and check the ending has a clear takeaway.
8. **Save and verify** — write the final Markdown once, then read it back.

## Output Shape

A Markdown draft with a title, hook, sections, citations, and a short "sources"
list. Optionally a DOCX/PDF via the `docx` or `pdf` skill.

## Recovery

- No sources found: say so and offer an opinion piece with clearly labeled
  analysis instead of fake citations.
- User voice is unclear: ask for one paragraph they like.
- Draft drifts from the outline: return to the approved section list.
- Length is over budget: cut the weakest section, not the evidence.
