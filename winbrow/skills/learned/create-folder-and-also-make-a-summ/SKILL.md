---
name: create-folder-and-also-make-a-summ
skill: create_folder_and_also_make_a_summ
args: []
description: >
  Create a new folder named 'and also make a summary of the paper in a text
  file and put it also in the downloads folder' on the Desktop.
---

## When to Use

Use when GOAL matches this learned capability: Create a new folder named 'and also make a summary of the paper in a text file and put it also in the downloads folder' on the Desktop.

## When NOT to Use

- Do not use it when a built-in skill already covers the same operation.
- Do not use it for a different app, path or target than the one described.
- Do not use it to work around a failure without reading the error first.
- Do not repeat it after a successful run with identical arguments.

## Step Pattern

1. Confirm the request matches this learned tool rather than a generic skill.
2. Call `create_folder_and_also_make_a_summ` with the required arguments.
3. Read the returned output and verify it against GOAL.
4. If a follow-up action is needed, use the path or value returned here.
5. If the tool fails, call repair-tool with its name and the failure text.

## Args Detail

This learned tool takes no arguments.

## Output Shape

Returns a result dict with `success` and `output`. A successful call means the
script completed; it does not by itself prove the wider goal is complete.

## Recovery

If the script errors, call repair-tool with the exact failure. If it succeeds
but returns the wrong result, treat it as a failure and do not repeat it
blindly. If the capability is no longer needed, switch to the matching
built-in skill. Follow the self-correct rule.
