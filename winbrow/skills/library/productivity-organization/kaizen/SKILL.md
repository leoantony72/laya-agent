---
name: kaizen
skill: playbook_kaizen
kind: playbook
category: productivity-organization
source: https://github.com/NeoLabHQ/context-engineering-kit/tree/master/plugins/kaizen/skills/kaizen
license: see upstream repository
args: []
description: >
  Apply continuous-improvement thinking to a process, codebase, or workflow.
  Use for small, reversible improvements instead of a big rewrite.
---

# Kaizen

## When to Use

Use when GOAL asks to improve a process, reduce waste, simplify a design, or
make an incremental change to a workflow or codebase.

## When NOT to Use

- Do not use for an emergency fix; use `root-cause-tracing`.
- Do not use to justify a rewrite in the name of improvement.
- Do not use for a greenfield design; use `brainstorming`.
- Do not measure only output volume; include quality and rework.

## Step Pattern

1. **Observe** — map the current process as it actually runs, not as documented.
   Identify the step, handoff, or waiting time.
2. **Measure** — pick one or two metrics: cycle time, defect rate, rework,
   manual steps, or cost. Record a baseline.
3. **Find waste** — waiting, overproduction, extra processing, transport,
   inventory, motion, defects, unused talent.
4. **Choose one small change** — reversible, testable in a short cycle, owned by
   one person.
5. **Predict** — state the expected effect before running the experiment.
6. **Run the experiment** — time-box it; keep the rest of the process stable.
7. **Check** — compare to the baseline. If it failed, record why and revert.
8. **Standardize** — update the checklist, template, or code so the improvement
   persists.
9. **Repeat** — choose the next constraint, not the next idea.

## Output Format

```
Current state → Baseline → Waste/constraint → One change → Prediction →
Result → Standardize or revert → Next constraint
```

## Rules

- Prefer one change at a time; do not bundle unrelated improvements.
- Keep a rollback path for code and process changes.
- Do not claim an improvement without before/after evidence.
- Involve the person who does the work; their tacit knowledge beats theory.
- Avoid over-engineering: the smallest change that removes the constraint wins.

## Recovery

- No baseline: measure the current cycle once before changing anything.
- The experiment is inconclusive: keep the change out, record the confounder,
  and rerun with one variable.
- The change makes another step worse: revert and trace the dependency.
- The team cannot agree on a metric: pick a temporary proxy and revisit it after
  one cycle.
