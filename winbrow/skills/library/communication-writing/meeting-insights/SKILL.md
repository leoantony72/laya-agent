---
name: meeting-insights
skill: playbook_meeting_insights
kind: playbook
category: communication-writing
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Analyze a meeting transcript for speaking ratios, filler words, questions,
  and communication patterns. Use for coaching and meeting review.
---

# Meeting Insights

## When to Use

Use when GOAL supplies a transcript or recording notes and asks for
communication feedback, speaking balance, filler words, conflict avoidance, or
leadership patterns.

## When NOT to Use

- Do not use to diagnose a person's mental health, personality, or competence.
- Do not use for a performance review decision; provide observations, not a
  verdict.
- Do not analyze a recording without the participants' consent.
- Do not invent timestamps or quotes.

## Prerequisites

- `run_skill_script` with `skill="meeting-insights"` and
  `script="transcript_metrics.py"`.
- Transcript lines should be `Speaker: text`, optionally with timestamps.
- `read_document` for DOCX/PDF transcripts.

## Step Pattern

1. Obtain the transcript. If it is a file, read it with `read_document` first.
2. Call `run_skill_script` with
   `{"operation":"analyze","path":"C:\\...\\meeting.vtt"}` or
   `{"content":"Alice: ...\\nBob: ..."}`.
3. Read the metrics: words, turns, speaking share, questions, fillers, hedges,
   and conflict-avoidance phrases.
4. Add qualitative observations only when supported by quoted lines. Include
   the timestamp or the exact line.
5. Structure the feedback:
   - What worked
   - What to try next time
   - One pattern to watch
   - Evidence table
6. Separate the facilitator's behavior from the group's behavior.
7. If the user asked for a file, save the report and read it back.

## Interpretation Rules

- Speaking share is not a quality score. A quiet facilitator may be effective.
- Filler words are normal; report rate, not a character judgment.
- A question count does not prove listening; look at follow-ups.
- Conflict avoidance is a hypothesis, not a diagnosis. Quote the evidence.
- Use neutral language: "you spoke 62% of the words", not "you dominated".

## Output Shape

A short report with metrics, quoted evidence, and 3–5 actionable suggestions.
The script also returns a `data` object with per-speaker numbers.

## Recovery

- No speaker labels: analyze the whole transcript and say the speaker split is
  unavailable.
- Very short transcript: report that the sample is too small for patterns.
- Sensitive content: summarize patterns, not personal disclosures.
