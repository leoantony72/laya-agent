---
name: solo-founder
skill: playbook_solo_founder
kind: playbook
category: productivity-organization
source: https://github.com/rockscy/solo-skills
license: see upstream repository
args: []
description: >
  Solo-founder workflows: launch posts, customer emails, ship decisions,
  postmortems, standups, user-bug triage, and changelogs.
---

# Solo Founder Workflows

## When to Use

Use when the user is running a small product alone and needs one of the seven
workflows below: launch/announcement, customer email, ship-or-wait decision,
postmortem, standup, user-reported bug, or changelog from commits.

## When NOT to Use

- Do not use for a large-team process that needs formal approvals.
- Do not use for legal, tax, or HR advice.
- Do not invent customer quotes, metrics, or shipped features.
- Do not use a workflow that does not match the task; load one reference only.

## Step Pattern

1. Classify the request and read exactly one reference:
   - Launch post or announcement → `references/launch-tweet.md`
   - Customer email → `references/customer-email.md`
   - Ship now vs wait → `references/ship-decision.md`
   - Incident or missed launch → `references/postmortem.md`
   - Weekly standup → `references/standup.md`
   - User-reported bug → `references/bug-from-user.md`
   - Release notes from commits → `references/changelog-from-commits.md`
2. Gather the minimum facts: audience, product, change, date, and desired
   action.
3. Follow the reference's output format. Keep it short; solo founders ship.
4. Save the draft when the user wants a file, then read it back.
5. End with the next concrete action.

## Reference Files

- `references/launch-tweet.md`
- `references/customer-email.md`
- `references/ship-decision.md`
- `references/postmortem.md`
- `references/standup.md`
- `references/bug-from-user.md`
- `references/changelog-from-commits.md`

## Output Shape

A short Markdown draft or decision note, plus one next action.

## Recovery

- Facts missing: ask for the smallest missing fact, not a full brief.
- User wants a different tone: ask for one example they like.
- Claim is unverifiable: remove it or label it as a hypothesis.
