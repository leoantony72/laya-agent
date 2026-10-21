---
name: remember-fact
skill: remember_fact
args:
  - name: fact
    type: string
    required: true
    description: A complete, durable fact phrased as a sentence.
description: >
  Save a durable user preference or environment fact for future commands. Use
  when the user states a lasting preference, path or setting, not for one-off
  task data. Do not use it to save secrets or transient values.
---

## When to Use

Use when the user says "remember", "from now on" or states a lasting fact such
as an exports folder, preferred browser or default download location. The fact
will be available to later sessions and can be recalled with recall-memory.

## When NOT to Use

- Do not save passwords, API keys, tokens or other secrets.
- Do not save transient task values that only matter for the current run; put
  them in HISTORY by completing the step.
- Do not save an uncertain guess; confirm the fact first.
- Do not use it to search past activity; use recall-memory.

## Step Pattern

1. Extract the durable fact from the user's wording.
2. Phrase it as one complete sentence, preserving exact paths and names.
3. Call remember-fact with that sentence.
4. Confirm to the user that the fact was saved.
5. Later tasks may use recall-memory to retrieve it.

## Args Detail

- `fact` must be a complete sentence with enough context to stand alone, for
  example "The user's exports folder is D:\Exports".
- Preserve paths exactly, including drive letters and backslashes.
- If the statement is ambiguous or conditional, emit clarify instead of saving
  a wrong fact.

## Output Shape

Returns `success` and `output` confirming the remembered fact. The fact is
stored in the memory database for future sessions.

## Recovery

If the save fails, report the failure instead of pretending the fact persists.
If the statement was ambiguous, ask a short clarifying question. Follow the
self-correct rule and never save a secret by accident.
