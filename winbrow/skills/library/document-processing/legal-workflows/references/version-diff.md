# Contract Version Diff

## Goal

Compare two versions of a contract and surface every change that could affect
risk, obligations, money, timing, or termination.

## Method

1. Read both documents with `read_document`.
2. Align by section heading, not by line number.
3. For each section, classify the change:
   - **Substantive** — changes rights, obligations, money, liability, term.
   - **Clarifying** — same meaning, clearer wording.
   - **Formatting** — numbering, defined-term capitalization, typos.
   - **Removed** — present in A, absent in B.
   - **Added** — absent in A, present in B.
4. Quote both versions for every substantive change.
5. Flag defined terms that changed meaning, even if the definition looks small.

## Output Format

| Section | Version A | Version B | Type | Impact | Recommended action |
|---|---|---|---|---|---|

Then:
- **Deal-impact summary** — what a business reader must know.
- **Silent risks** — changes that look cosmetic but alter liability or scope.
- **Open questions** — missing pages, unreadable text, undefined terms.

## Rules

- Do not infer intent from a change; describe the text.
- If one version is a scan with no extractable text, say so and stop.
- If numbering differs, cite headings and quoted text rather than numbers.
- Do not produce a "redline" file unless the user asked for one and a tool
  exists; a Markdown comparison is not a Word redline.
