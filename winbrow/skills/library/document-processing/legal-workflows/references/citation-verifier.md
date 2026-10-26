# Citation Verifier

## Goal

Check that every legal citation in a document exists, says what the document
claims, and is still good law enough for the user's purpose.

## Verification Steps

For each citation:

1. **Parse** the citation into court, year, reporter, docket, or statute.
2. **Existence** — confirm the source exists in an official reporter, court
   site, legislature site, or reputable database.
3. **Quote match** — compare the quoted language to the source; flag any
   paraphrase presented as a quote.
4. **Proposition match** — confirm the cited passage supports the proposition
   in the document.
5. **Status** — check for later history: affirmed, reversed, overruled,
   amended, superseded, or repealed. If status cannot be verified, say so.
6. **Jurisdiction** — confirm the authority applies in the relevant
   jurisdiction and court level.

## Output Format

| Citation | Exists? | Quote accurate? | Supports claim? | Status | Confidence | Source |
|---|---|---|---|---|---|---|

Then list:
- **Broken citations** — nonexistent or mismatched.
- **Unverified citations** — could not be checked.
- **Proposition drift** — real source, wrong claim.

## Rules

- Never invent a reporter cite, docket number, URL, or holding.
- If web access is unavailable, mark the citation "unverified" rather than
  guessing.
- Prefer official sources over summaries and blogs.
- This is a verification aid, not a legal opinion.
