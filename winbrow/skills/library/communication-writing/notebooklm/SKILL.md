---
name: notebooklm
skill: playbook_notebooklm
kind: playbook
category: communication-writing
source: https://github.com/PleasePrompto/notebooklm-skill
license: see upstream repository
args: []
description: >
  Query a NotebookLM notebook through the browser for source-grounded answers.
  Use when the user already has notebooks and wants citations from them.
---

# NotebookLM

## When to Use

Use when GOAL asks to query a NotebookLM notebook, compare its sources, or get
an answer grounded only in documents the user uploaded there.

## When NOT to Use

- Do not use for a general web question; use normal browser research.
- Do not use without the user's NotebookLM session; never ask for a password.
- Do not upload confidential documents without explicit permission.
- Do not present a NotebookLM answer as verified fact; it is grounded in the
  user's sources, which may be wrong.

## Prerequisites

- The user is signed in to NotebookLM in the Chrome/Edge profile the agent
  controls.
- Browser tools (`browser_navigate`, `browser_read_full`, `browser_interact`).
- If the page asks for a login, stop and ask the user to sign in manually.

## Step Pattern

1. Ask for the notebook URL or name if it is not in GOAL.
2. `browser_navigate("https://notebooklm.google.com/")` and read the page.
3. Open the requested notebook. If the list is ambiguous, ask which one.
4. Read the notebook page to confirm the source count and titles.
5. Ask the question in the notebook chat. Use one question per turn.
6. Read the answer and the cited source chips. Open the cited passage when the
   claim is important.
7. Report the answer with: question, answer, cited sources, and any limitation
   (missing source, partial answer, or notebook refusal).
8. Do not copy the whole notebook; summarize.

## Output Shape

A cited answer with the notebook name and source titles. If the user wants a
file, save it as Markdown and read it back.

## Recovery

- Login wall: stop and ask the user to sign in; do not automate credentials.
- Notebook not found: list the visible notebooks and ask which one.
- Answer has no citations: say it is ungrounded and ask whether to continue.
- UI changed or selectors fail: report the page state and fall back to asking
  the user to paste the answer.
