# NDA Triage

## Goal

Produce a fast, consistent first-pass review of a confidentiality agreement.
This is triage, not legal advice.

## Checklist

Review these clauses and record the exact section number and quoted language:

1. **Parties** — correct legal names, affiliates covered?
2. **Definition of confidential information** — broad enough, or unlimited?
3. **Exclusions** — independently developed, publicly available, lawfully received.
4. **Obligations** — reasonable care, access limits, need-to-know.
5. **Permitted disclosures** — legal compulsion, affiliates, advisers.
6. **Term and survival** — duration, trade-secret carve-out.
7. **Return/destruction** — timing, backups and legal-hold exceptions.
8. **Remedies** — injunction language, no bond, fee shifting.
9. **Assignment** — change of control, affiliates.
10. **Governing law and venue** — acceptable jurisdiction.
11. **Residuals / non-solicit / non-compete** — flag any unusual restriction.
12. **Signature block** — entity, title, authority.

## Risk Levels

- **Green** — mutual, balanced, standard exclusions, reasonable term.
- **Amber** — one-sided obligations, long term, broad definition, missing
  return/destruction detail.
- **Red** — perpetual confidentiality, unlimited damages, assignment without
  consent, non-compete, or a jurisdiction the user flagged as unacceptable.

## Output Format

| # | Clause | Current language | Risk | Why it matters | Suggested fallback |
|---|---|---|---|---|---|

Then:
- **Top three issues**
- **Missing information / questions for the user**
- **Not legal advice**

## Rules

- Never mark a clause "standard" without quoting it.
- If the document is not an NDA, say so and stop.
- If the counterparty is a government or regulated entity, flag that special
  rules may apply.
