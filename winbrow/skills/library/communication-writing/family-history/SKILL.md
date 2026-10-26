---
name: family-history
skill: playbook_family_history
kind: playbook
category: communication-writing
source: https://github.com/emaynard/claude-family-history-research-skill
license: see upstream repository
args: []
description: >
  Plan and organize genealogical research with source citations and proof
  standards. Use for family trees, records, and ancestor questions.
---

# Family History Research

## When to Use

Use when GOAL asks about ancestors, family records, genealogy, a family tree,
or how to verify a family story.

## When NOT to Use

- Do not use for a living person's sensitive data without a clear reason.
- Do not use for legal identity verification or DNA medical interpretation.
- Do not present a family story as fact without a record.
- Do not scrape a paid genealogy site against its terms.

## Step Pattern

1. **Define the question** — name, place, date range, and relationship. Vague
   questions produce vague research.
2. **List known facts** — from the user, documents, or prior research. Separate
   documented facts from family lore.
3. **Choose record types** — census, birth/marriage/death, immigration, land,
   military, newspaper, cemetery, church, DNA matches. Start with the most
   specific and most available.
4. **Search** — use public archives, library catalogs, and permitted sites via
   the browser. Record the collection, record ID, image number, and URL.
5. **Cite every fact** — source title, repository, date, page/image, and what
   it actually says. A name match alone is not proof.
6. **Resolve conflicts** — when two records disagree, list both and explain
   which is stronger and why.
7. **Build a timeline** — date, event, place, source, confidence.
8. **Plan next steps** — the next record to request, the question it answers,
   and the cost/access route.

## Proof Standards

- **Primary** — created at the event by a witness (birth record, census).
- **Secondary** — created later from memory or derivative sources.
- **Direct** — states the fact explicitly.
- **Indirect** — requires inference from other facts.
- Mark conclusions as `proven`, `probable`, `possible`, or `disproven`.

## Output Shape

A research plan, timeline table, and source list. Save as Markdown when the user
wants a file.

## Recovery

- Record not found: report the exact collection searched and the index used.
- Name too common: add place, date, and associates to disambiguate.
- Conflicting records: present both; do not pick one silently.
- Privacy concern: redact living individuals unless the user asks otherwise.
