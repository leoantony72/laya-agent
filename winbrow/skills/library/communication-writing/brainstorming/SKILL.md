---
name: brainstorming
skill: playbook_brainstorming
kind: playbook
category: communication-writing
source: https://github.com/obra/superpowers/tree/main/skills/brainstorming
license: see upstream repository
args: []
description: >
  Turn a rough idea into a reviewed design through questions and alternatives.
  Use before any new feature, project, or creative deliverable.
---

# Brainstorming

## When to Use

Use when the user has a vague goal, asks to "figure out" what to build, or
starts a feature, project, talk, business, or renovation without a clear
design. Use before implementation, not after.

## When NOT to Use

- Do not use for a small, fully specified task; just do it.
- Do not use when the user explicitly says no questions or "just build it".
- Do not use to delay a decision the user has already made.
- Do not use for debugging; use `root-cause-tracing`.

## Step Pattern

1. **Check what is known.** Read the request and any supplied files first. If
   the goal is already precise enough to act on, say so and act.
2. **Ask one question at a time.** Focus on the problem, the user, the
   constraint, and what success looks like. Stop when the next question would
   not change the design.
3. **Lay out 2–3 approaches.** For each: what it is, what it optimizes, what it
   costs, and when it is the right choice. Do not recommend before the user
   sees the tradeoffs.
4. **Get a choice.** Ask the user to pick an approach or combine them.
5. **Write a short design doc** (one page) with: problem, users, goals,
   non-goals, chosen approach, data/flow, risks, acceptance checks.
6. **Get approval** of the doc before implementation. If the user changes the
   design later, update the doc.
7. **Hand off** to the actual work with the next concrete action.

## Rules

- Prefer questions over assumptions when a wrong assumption would be costly.
- Do not turn brainstorming into a questionnaire; 3–6 questions is usually
  enough.
- Include a "do nothing" option when it is viable.
- For software, identify the smallest shippable slice.
- For non-software work, identify the audience and the artifact.
- Record decisions in the design doc; do not rely on chat history alone.

## Output Shape

A one-page Markdown design doc, plus a short summary of the chosen approach and
the next action. Save it with `create_text_file` when the user wants a file.

## Recovery

- User cannot answer: offer three concrete options and a default.
- Scope grows: split into a first slice and a later phase.
- User rejects all approaches: ask what constraint is missing.
