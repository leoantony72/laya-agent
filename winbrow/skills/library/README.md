# Converted Skill Library

This folder contains 43 skills converted from the six requested sections of
[ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills).
No skills from Development, Business, Security, Assistive Technology, or App
Automation were used.

## How these skills work

Each skill is a normal brain skill with one intentional difference:

```yaml
skill: playbook_docx
kind: playbook
```

A `playbook_*` skill is guidance, not a tool. When the planner selects it, the
runtime returns an acknowledgement and injects the full `SKILL.md` body as
`SKILL DETAIL` on the next turn. The planner then executes the concrete tools
named in the body (`read_document`, browser tools, `run_skill_script`, ...).

This preserves progressive disclosure: only the compact trigger line sits in the
cached prompt until the skill is chosen.

## Layout

```
library/
  <section>/<skill>/SKILL.md
  <section>/<skill>/scripts/*.py     # only where a real file operation is needed
  <section>/<skill>/references/*.md  # on-demand detail for packs and themes
  <section>/<skill>/templates/*.md
```

Sections:

- `document-processing`
- `data-analysis`
- `communication-writing`
- `creative-media`
- `productivity-organization`
- `collaboration-project-management`

## Bundled script runner

Scripts run through the native `run_skill_script` tool:

```json
{
  "skill": "docx",
  "script": "docx_tool.py",
  "spec": {"operation": "create", "output_path": "C:\\...\\report.docx", "title": "Report", "content": "# Summary\\n..."}
}
```

Safety properties:

- only files under this library are executable;
- skill/script names are validated;
- path traversal and symlinks are rejected;
- the spec is passed as JSON, not through a shell;
- a hard timeout and output cap apply;
- the tool is T2, so writes still go through the permission gate.

## Optional dependencies

See `requirements-skills.txt`. Missing packages are reported by each script with
an install command for the exact interpreter running the agent, and the
Integrations panel shows their status.

## Credentials and services

Add keys and connections in the WinBrow **Integrations** panel (header button).
Secrets are stored in `data/integrations.json`, encrypted with Windows DPAPI
where available, masked on every read, and never written into a SKILL.md.

Gated skills: `deep-research`, `postgres`, `notebooklm`, `imagen`,
`pixelbin-media`, `google-workspace`, `outline`, `n8n-workflows`, `mercury`.

## Provenance and licensing

See `PROVENANCE.md`. Several upstream skills are proprietary or carry their own
license. The converted files are rewritten for WinBrow; they are not verbatim
copies of upstream scripts or text.
