---
name: legal-workflows
skill: playbook_legal_workflows
kind: playbook
category: document-processing
source: https://github.com/sboghossian/master-claude-for-legal
license: see upstream repository
args: []
description: >
  Legal-team workflows: NDA triage, contract version diff, citation checks,
  meeting briefs, status synthesis, and tabular review.
---

# Legal Workflows

## When to Use

Use for legal-operations work: reviewing an NDA, comparing contract versions,
verifying citations, preparing a meeting brief, synthesizing a status update,
or reviewing a set of documents in a table. Use only when the user is working
with legal or compliance material.

## When NOT to Use

- Do not give legal advice or predict a court outcome.
- Do not use for general writing, marketing, or code tasks.
- Do not process privileged material without confirming the user wants it
  handled locally.
- Do not invent a clause, citation, statute, or case.

## Prerequisites

- Read the relevant reference file from this skill's `references/` folder with
  `read_document`. Read only the one that matches the task.
- Use `read_document` for DOCX/PDF inputs and `mcp_filesystem_write_file` or
  `create_text_file` for outputs.
- For multi-document comparison, keep each document's extracted text in
  HISTORY and cite filenames in the result.

## Step Pattern

1. Classify the request and load exactly one reference:
   - NDA or confidentiality review → `references/nda-triage.md`
   - Two or more contract versions → `references/version-diff.md`
   - Citation or authority check → `references/citation-verifier.md`
   - Meeting preparation → `references/meeting-brief.md`
   - Weekly/status update → `references/status-synthesis.md`
   - Many documents in a grid → `references/tabular-review.md`
2. Read the source documents with `read_document(path, max_chars=40000)`.
3. Follow the reference's output format. Separate facts from assumptions and
   flag missing information explicitly.
4. Write the deliverable to a named file when the user asked for one, then read
   it back. Otherwise answer in chat.
5. End with a short "Not legal advice" note when the output could be mistaken
   for a legal opinion.

## Reference and Template Files

- `references/nda-triage.md`
- `references/version-diff.md`
- `references/citation-verifier.md`
- `references/meeting-brief.md`
- `references/status-synthesis.md`
- `references/tabular-review.md`
- `templates/client-data-explainer.md`
- `templates/firm-ai-policy.md`
- `templates/vendor-security-questionnaire.md`

## Output Shape

A structured Markdown answer or a saved `.md`/`.docx` file. Every finding must
point to the document and section it came from.

## Recovery

- Missing document: ask for it; never reconstruct a contract from memory.
- Ambiguous clause: present the exact language and ask which interpretation
  the user wants.
- Conflicting versions: list the differences and ask which version controls.
- Citation cannot be verified: mark it "unverified" and stop; never fill in a
  plausible citation.
