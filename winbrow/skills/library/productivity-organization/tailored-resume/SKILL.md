---
name: tailored-resume
skill: playbook_tailored_resume
kind: playbook
category: productivity-organization
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Tailor a resume to a job description with ATS-aware keywords and evidence.
  Use when the user is applying for a specific role.
---

# Tailored Resume

## When to Use

Use when GOAL supplies a job description and a resume, and asks to tailor,
rewrite, or optimize the resume for that role.

## When NOT to Use

- Do not invent experience, titles, dates, degrees, or metrics.
- Do not keyword-stuff or hide text.
- Do not change employment dates or employers.
- Do not submit the resume anywhere; the user submits it.

## Step Pattern

1. Read both files with `read_document` (job description first, then resume).
2. Extract from the job description:
   - must-have skills and years
   - responsibilities
   - tools and domain terms
   - seniority and scope signals
3. Map the resume's real evidence to each requirement. Mark:
   - **strong** — direct evidence
   - **partial** — adjacent evidence
   - **gap** — no evidence
4. Rewrite the summary and the most relevant bullets:
   - lead with the result, then the action and method
   - use the employer's vocabulary where it is truthful
   - keep one bullet to one achievement
5. Keep the original structure unless it is clearly broken. ATS prefers simple
   headings, standard fonts, and no tables/columns.
6. Add a short gap note: what to learn or how to address it in an interview.
7. Save the tailored version as a new file; never overwrite the original.
8. If asked, generate a DOCX/PDF with the `docx` or `pdf` skill and verify it.
   - PDF: use the `pdf` skill with `document_kind="resume"`, `page_size="a4"`
     (or `"letter"` for US paper), `page_numbers=false`, portrait. Do not use a
     16:9 slide deck for a resume.
   - If a TeX engine is installed and the user wants maximum typographic
     control, use the `pdf` skill's `latex` operation; otherwise use the
     measured `create` engine.

## Output Shape

A tailored Markdown resume, a match table (requirement → evidence → strength),
and a gap list. Optionally a DOCX/PDF.

## Recovery

- Job description is an image: OCR it first.
- Resume has no measurable results: ask for numbers rather than inventing them.
- User asks for a different role in the same file: keep versions separate.
- ATS format is unknown: use a single-column, standard-heading layout.
