# Changelog from Commits

## Goal

Turn technical commits into user-facing release notes.

## Method

1. Get the commit range (`git log` via the `git` MCP server or `run_command`).
2. Group commits into:
   - New features
   - Improvements
   - Fixes
   - Breaking changes
   - Internal-only (omit from the user changelog)
3. For each user-facing item, write:
   - what changed
   - why it matters
   - any action the user must take
4. Link the issue/PR when available.
5. Add a short "upgrade notes" section for breaking changes.
6. Verify every claim against the diff; do not infer a feature from a commit
   title alone.

## Output Format

```
# <version> — <date>

## New
- ...

## Improved
- ...

## Fixed
- ...

## Breaking changes
- ...
```

## Rules

- No internal refactors, dependency bumps, or test-only changes.
- No marketing adjectives.
- If a commit is ambiguous, read the diff or omit it.
- Keep the user changelog shorter than the raw git log.
