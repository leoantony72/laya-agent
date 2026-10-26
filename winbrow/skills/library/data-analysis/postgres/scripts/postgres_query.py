"""Read-only PostgreSQL queries with a hard transaction-level write block."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, integration_secret, load_spec, require_module

psycopg2 = require_module("psycopg2", "psycopg2-binary")

_READ_ONLY = re.compile(r"^\s*(select|with|show|explain|values)\b", re.I)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"call|do|vacuum|analyze|refresh|reindex|listen|notify)\b",
    re.I,
)


def _connection(spec: dict) -> dict:
    connection = spec.get("connection")
    if isinstance(connection, dict):
        return connection
    name = str(spec.get("connection_name") or "").strip()
    inline = integration_secret("postgres", "connections_json")
    if inline:
        try:
            data = json.loads(inline)
        except Exception as exc:
            fail(f"Invalid connections JSON in Integrations: {exc}")
        databases = data.get("databases") if isinstance(data, dict) else data
        if isinstance(databases, list):
            for item in databases:
                if str(item.get("name", "")).lower() == name.lower():
                    return item
            if databases:
                return databases[0]
    path_value = (
        spec.get("connections_path")
        or integration_secret("postgres", "connections_path")
        or os.environ.get("WINBROW_POSTGRES_CONNECTIONS")
        or ""
    )
    if path_value:
        path = Path(os.path.expandvars(os.path.expanduser(str(path_value))))
        if not path.is_file():
            fail(f"PostgreSQL connections file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"Invalid connections JSON: {exc}")
        databases = data.get("databases") if isinstance(data, dict) else data
        if isinstance(databases, list):
            for item in databases:
                if str(item.get("name", "")).lower() == name.lower():
                    return item
            if databases:
                return databases[0]
    dsn = str(spec.get("dsn") or os.environ.get("WINBROW_POSTGRES_DSN") or "")
    if dsn:
        return {"dsn": dsn}
    fail(
        "PostgreSQL is not configured. Add a connection in the Integrations "
        "panel or set WINBROW_POSTGRES_DSN."
    )


def _connect(connection: dict):
    kwargs = {}
    if connection.get("dsn"):
        return psycopg2.connect(connection["dsn"], connect_timeout=10)
    for key in ("host", "port", "dbname", "database", "user", "password", "sslmode"):
        value = connection.get(key)
        if value not in (None, ""):
            kwargs["dbname" if key == "database" else key] = value
    kwargs["connect_timeout"] = 10
    return psycopg2.connect(**kwargs)


def _run_read_only(connection: dict, query: str, params=None, limit: int = 200):
    conn = _connect(connection)
    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute(query, params)
                if cursor.description is None:
                    return [], []
                columns = [item[0] for item in cursor.description]
                rows = cursor.fetchmany(limit)
                return columns, rows
    finally:
        conn.close()


def test_connection(spec: dict) -> None:
    connection = _connection(spec)
    columns, rows = _run_read_only(connection, "SELECT version() AS version, current_database() AS database")
    emit(True, "PostgreSQL connection succeeded.", data={
        "columns": columns,
        "rows": [[str(value) for value in row] for row in rows],
    })


def query(spec: dict) -> None:
    raw = str(spec.get("query") or "").strip()
    if not raw:
        fail("A SQL query is required.")
    # One statement only: a semicolon is allowed at the very end.
    stripped = raw.rstrip().rstrip(";")
    if ";" in stripped:
        fail("Only one read-only SQL statement is allowed per call.")
    if not _READ_ONLY.match(stripped):
        fail("Only SELECT, WITH, SHOW, EXPLAIN, or VALUES queries are allowed.")
    if _FORBIDDEN.search(stripped):
        fail("The query contains a write or administrative keyword. This tool is read-only.")
    limit = max(1, min(int(spec.get("limit") or 200), 1000))
    connection = _connection(spec)
    columns, rows = _run_read_only(connection, stripped, spec.get("params"), limit)
    text = "\t".join(columns) + "\n" + "\n".join(
        "\t".join("" if value is None else str(value) for value in row)
        for row in rows
    )
    emit(True, text or "Query returned no rows.", data={
        "columns": columns,
        "rows": [[None if value is None else str(value) for value in row] for row in rows],
        "count": len(rows),
    })


def tables(spec: dict) -> None:
    connection = _connection(spec)
    columns, rows = _run_read_only(
        connection,
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
        "ORDER BY table_schema, table_name",
        limit=500,
    )
    text = "\n".join(f"{row[0]}.{row[1]}" for row in rows)
    emit(True, text or "No user tables found.", data={"count": len(rows)})


def schema(spec: dict) -> None:
    table = str(spec.get("table") or "").strip()
    if not table:
        fail("A table name is required.")
    connection = _connection(spec)
    columns, rows = _run_read_only(
        connection,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = %s "
        "ORDER BY ordinal_position",
        (table.split(".")[-1],),
        limit=500,
    )
    text = "\n".join(
        f"{row[0]}  {row[1]}  nullable={row[2]}  default={row[3]}" for row in rows
    )
    emit(True, text or f"No columns found for {table}.", data={"count": len(rows)})


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "query").strip().lower()
    handlers = {
        "test": test_connection,
        "query": query,
        "tables": tables,
        "list_tables": tables,
        "schema": schema,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(f"Unknown postgres operation '{operation}'. Use test, query, tables, or schema.")
    handler(spec)


if __name__ == "__main__":
    main()
