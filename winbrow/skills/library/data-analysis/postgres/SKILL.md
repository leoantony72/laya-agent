---
name: postgres
skill: playbook_postgres
kind: playbook
category: data-analysis
source: https://github.com/sanjay3290/ai-skills/tree/main/skills/postgres
license: Apache-2.0
args: []
description: >
  Run safe read-only SQL against a configured PostgreSQL database. Use for
  schema exploration and SELECT analysis, never writes.
---

# PostgreSQL Read-Only Queries

## When to Use

Use when GOAL asks to inspect a PostgreSQL schema, list tables, run a SELECT
for analysis, or check database contents.

## When NOT to Use

- Do not use for inserts, updates, deletes, DDL, migrations, or admin work.
- Do not use for MySQL, SQL Server, SQLite, or a CSV file.
- Do not use with production credentials the user has not explicitly approved.
- Do not query personal or regulated data without a stated purpose.

## Prerequisites

- A PostgreSQL connection in the Integrations panel, or
  `WINBROW_POSTGRES_DSN`, or a connections JSON file.
- `run_skill_script` with `skill="postgres"` and `script="postgres_query.py"`.
- `psycopg2-binary` must be installed. The script reports the exact install
  command if it is missing.

## Step Pattern

1. If available, call `integration_status("postgres")`; if unconfigured, ask
   the user to add a connection and stop.
2. Discover before querying:
   - `{"operation":"tables"}` to list user tables.
   - `{"operation":"schema","table":"public.orders"}` for columns and types.
3. Run one read-only query:
   `{"operation":"query","query":"SELECT status, count(*) FROM orders GROUP BY status","limit":200}`.
4. Read the rows and describe the result. Do not dump hundreds of rows into
   chat; summarize and show a small sample.
5. If the user asks for a file, write the result with `create_text_file` or
   `xlsx` and verify it.
6. State the database name and that the query was read-only.

## Safety Rules

- The script opens a `READ ONLY` transaction and sets a 15-second statement
  timeout.
- Only `SELECT`, `WITH`, `SHOW`, `EXPLAIN`, and `VALUES` are accepted.
- Write/admin keywords are rejected before execution.
- One statement per call. Remove trailing semicolons; internal semicolons are
  rejected.
- Never put a password in the chat, a SKILL.md, or a log line.

## Output Shape

Returns tab-separated text plus `data.columns`, `data.rows`, and `data.count`.

## Recovery

- Connection refused: check host/port and whether the database is running.
- Authentication failed: ask the user to re-enter credentials in Integrations.
- Permission denied on a table: report it; do not try another role.
- Query timeout: narrow the range or add a filter, then retry once.
