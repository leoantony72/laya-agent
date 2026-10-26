---
name: review-implementing
skill: playbook_review_implementing
kind: playbook
category: collaboration-project-management
source: https://github.com/mhattingpete/claude-skills-marketplace/tree/main/engineering-workflow-plugin/skills/review-implementing
license: see upstream repository
args: []
description: >
  Turn code-review comments into verified changes without scope creep. Use when
  the user provides PR feedback or asks to implement review notes.
---

# Review Implementing

## When to Use

Use when GOAL supplies reviewer comments, PR feedback, or a review checklist and
asks to implement or respond to it.

## When NOT to Use

- Do not use for a new feature request disguised as review feedback; clarify.
- Do not implement a comment you do not understand; ask.
- Do not change unrelated code to "make the diff cleaner".
- Do not claim a review item is fixed without a test or verification.

## Step Pattern

1. Read the review comments and the diff. Restate each item as a concrete change
   or a question.
2. Classify each item:
   - **must fix** — correctness, security, data loss, test failure
   - **should fix** — maintainability, clarity, missing test
   - **discuss** — design preference or tradeoff
   - **decline** — incorrect or out of scope, with a reason
3. Order by risk, not by comment order.
4. For each must/should fix:
   - make the smallest change
   - run the narrowest test
   - record the command and result
5. Reply to the review item with what changed and the evidence. Do not mark it
   resolved if the test did not run.
6. For discuss items, present the tradeoff and ask for a decision.
7. Run the full relevant test suite at the end and summarize.

## Output Shape

A review-response table: comment, classification, change, verification, status.
Plus a short summary of anything not implemented and why.

## Recovery

- Comment conflicts with the spec: ask which one wins.
- Test environment unavailable: report the blocker, do not fake a pass.
- Reviewer asks for a large refactor: split it into a follow-up.
- Conflicting comments: surface the conflict instead of guessing.
