---
name: ship-learn-next
skill: playbook_ship_learn_next
kind: playbook
category: productivity-organization
source: https://github.com/michalparkola/tapestry-skills-for-claude-code/tree/main/ship-learn-next
license: see upstream repository
args: []
description: >
  Turn an article, tutorial, or video into five shippable practice reps. Use
  when the user wants to act on learning instead of collecting notes.
---

# Ship-Learn-Next

## When to Use

Use when GOAL gives learning material (transcript, article, course, tutorial)
and asks what to build, how to practice, or how to turn it into action.

## When NOT to Use

- Do not use for reference notes the user only wants to store.
- Do not use for a task that needs a full project plan with dependencies.
- Do not invent a result the user did not achieve.
- Do not produce more than five reps; depth over volume.

## Step Pattern

1. Read or summarize the source. Extract the 3–7 actionable ideas.
2. Pick one idea with the highest leverage for the user's goal.
3. Design **five reps**, each shippable in 30–120 minutes:
   - Rep 1: reproduce the smallest example.
   - Rep 2: change one variable.
   - Rep 3: apply it to the user's own context.
   - Rep 4: add a constraint (time, scale, quality).
   - Rep 5: teach it or ship it to another person.
4. For each rep, specify:
   - the artifact (file, post, demo, note)
   - the definition of done
   - the feedback signal
   - the reflection question
5. Put the reps in order with realistic dates.
6. Save the plan as Markdown when the user asks, then read it back.
7. After each rep, ask what was learned and adjust the next one.

## Output Shape

A five-rep table plus a short "start here" instruction for rep 1.

## Recovery

- Source is too vague: ask for one concrete example.
- Rep is too big: cut it to a 30-minute version.
- User has no project: use a small public dataset or a personal workflow.
- User skips reflection: keep the question, but do not block the next rep.
