# Ship Decision

## Goal

Decide whether to ship now, wait for one more thing, or kill the change.

## Questions

1. What user problem does this solve, and how often does it occur?
2. What is the smallest version that delivers the value?
3. What can break, and who is affected?
4. Is the risk reversible? Can we feature-flag or roll back?
5. What evidence do we have that it works?
6. What is the cost of waiting one week?
7. What is the one thing that must be true before shipping?

## Decision Rule

- **Ship** — value is clear, the change is reversible, and the main risk is
  understood.
- **Ship behind a flag** — value is clear but exposure should be limited.
- **Wait** — one specific, testable gap remains and waiting is cheaper than a
  rollback.
- **Kill** — the problem is not important enough or the maintenance cost is too
  high.

## Output

A one-page decision: recommendation, evidence, risk, rollback, and the trigger
to revisit.
