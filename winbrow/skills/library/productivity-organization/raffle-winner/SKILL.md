---
name: raffle-winner
skill: playbook_raffle_winner
kind: playbook
category: productivity-organization
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Pick random raffle or giveaway winners from a list, CSV, or Excel file. Use
  when the user needs a fair, auditable selection.
---

# Raffle Winner Picker

## When to Use

Use when GOAL asks to pick one or more random winners from entries, a CSV, an
Excel file, or a list in chat.

## When NOT to Use

- Do not use to favor a specific person or exclude an eligible entry.
- Do not use for a lottery that requires a regulated drawing process.
- Do not use if the entry list is incomplete or duplicated without cleaning it.
- Do not promise legal fairness compliance; describe the method.

## Prerequisites

- `run_skill_script` with `skill="raffle-winner"` and
  `script="pick_winners.py"`.
- `openpyxl` for `.xlsx`; CSV and chat lists need no extra package.

## Step Pattern

1. Confirm the eligibility rules, the number of winners, and whether repeats
   are allowed.
2. Load entries:
   - file: `{"operation":"pick","path":"C:\\...\\entries.xlsx","count":3,"has_header":true}`
   - list: `{"entries":["Alice","Bob","Carol"],"count":1}`
3. The script uses `secrets.SystemRandom` by default. If the user provides a
   seed for a verifiable drawing, it uses `random.Random(seed)` and reports
   `seed_used: true`.
4. Verify the winner count and that no name appears twice when `unique` is true.
5. If asked, save the result CSV with `output_path` and read it back.
6. Report the method, the entry count, the winners, and the seed status.

## Output Shape

Returns `data.winners`, `data.entry_count`, `data.seed_used`, and an optional
`output_path`.

## Recovery

- Fewer entries than winners: stop and report the count.
- Duplicate entries: ask whether to de-duplicate by name/email before picking.
- Empty file: report the sheet/columns read and ask for the correct source.
- User disputes the result: show the entry count and the seed/method; do not
  redraw silently.
