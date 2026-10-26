# Task Checkpoint — Converted Skill Library + Integrations

## Goal

Convert the 43 entries from the six requested sections of
`ComposioHQ/awesome-claude-skills` into WinBrow-compatible skills, and add a UI
for the optional services those skills need.

## Status

DONE — all six phases implemented; focused tests passing.

## Scope

- 43 playbooks under `winbrow/skills/library/<section>/<name>/SKILL.md`.
- 21 bundled Python helpers, references, and templates.
- `kind: playbook` support, category-grouped metadata, 12k budget.
- `run_skill_script` (T2, allowlisted) and `integration_status` (T0).
- Integrations panel and backend for 8 providers + dependency status.
- 27 focused tests pass; 65 skill definitions load; metadata ~9,060 chars.

## Key decisions

- Playbooks are guidance-only; a playbook step is acknowledged and the body is
  injected as SKILL DETAIL on the next turn.
- Metadata budget raised from 4,000 to 12,000 chars because 43 entries no longer
  fit. Core descriptions stay longest; playbooks use 95 chars.
- Bundled scripts are invoked through one native runner, not shell-string
  interpolation. Specs travel as JSON; the runner is T2.
- Credentials live in `data/integrations.json`, DPAPI-encrypted on Windows,
  masked on read, env vars win. Gated skills must call `integration_status`
  first.
- Upstream proprietary skills (`docx`, `pdf`, `pptx`, `xlsx`) were rewritten,
  not copied. See `winbrow/skills/library/PROVENANCE.md`.

## Verification run in this session

- `python -m pytest winbrow/tests -q` → 27 passed.
- `python -m pytest test_agent.py test_brain_m0.py test_syntax.py -q` →
  48 passed, 1 pre-existing fixture error (`test_agent.py::test_api` has no
  `cmd` fixture).
- Smoke: PDF, PPTX, XLSX, EPUB, DOCX, CSV, PNG, GIF, and a playbook dispatch.
- `node` compile of both inline scripts → 2 scripts, 0 syntax errors.
- `load_skill_registry` → 65 total, 43 playbooks.

## Next actions

- Install optional packages as needed: `pip install -r
  winbrow/skills/library/requirements-skills.txt`.
- Configure credentials in the Integrations panel before using gated skills.
- Live-smoke the Google OAuth flow and the NotebookLM browser flow.
- Run the full repo suite when time allows; `test_agent.py::test_api` needs a
  fixture and is unrelated.
