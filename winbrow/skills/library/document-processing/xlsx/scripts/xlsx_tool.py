"""Create, edit, profile, and chart Excel .xlsx workbooks."""

from __future__ import annotations

from winbrow.skill_script_utils import (
    clip, emit, ensure_parent, fail, load_spec, require_module, resolve_path,
)

openpyxl = require_module("openpyxl", "openpyxl")
Workbook = openpyxl.Workbook
BAR = openpyxl.chart.BarChart
LINE = openpyxl.chart.LineChart
PIE = openpyxl.chart.PieChart
Reference = openpyxl.chart.Reference


def _coerce(value):
    if isinstance(value, str) and value.startswith("="):
        return value
    return value


def _write_rows(worksheet, rows) -> int:
    count = 0
    for row in rows or []:
        if not isinstance(row, (list, tuple)):
            continue
        worksheet.append([_coerce(value) for value in row])
        count += 1
    return count


def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".xlsx":
        output = output.with_suffix(".xlsx")
    ensure_parent(output)
    workbook = Workbook()
    default = workbook.active
    workbook.remove(default)

    sheets = spec.get("sheets") or []
    if not isinstance(sheets, list) or not sheets:
        sheets = [{"name": "Sheet1", "rows": spec.get("rows") or []}]
    for index, sheet_spec in enumerate(sheets):
        if not isinstance(sheet_spec, dict):
            continue
        name = str(sheet_spec.get("name") or f"Sheet{index + 1}")[:31]
        worksheet = workbook.create_sheet(title=name)
        headers = sheet_spec.get("headers") or []
        if headers:
            worksheet.append([str(value) for value in headers])
        _write_rows(worksheet, sheet_spec.get("rows") or [])
        if not headers and not sheet_spec.get("rows"):
            worksheet["A1"] = "Value"
    workbook.save(str(output))
    emit(True, f"Created workbook: {output}", path=output, count=len(sheets))


def append_rows(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("output_path") or "")
    if not path.is_file():
        fail(f"Workbook not found: {path}")
    workbook = openpyxl.load_workbook(str(path))
    name = str(spec.get("sheet") or workbook.sheetnames[0])
    worksheet = workbook[name] if name in workbook.sheetnames else workbook.create_sheet(name)
    count = _write_rows(worksheet, spec.get("rows") or [])
    workbook.save(str(path))
    emit(True, f"Appended {count} row(s) to {name}", path=path, count=count)


def set_cells(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Workbook not found: {path}")
    workbook = openpyxl.load_workbook(str(path))
    name = str(spec.get("sheet") or workbook.sheetnames[0])
    worksheet = workbook[name]
    changed = 0
    for item in spec.get("cells") or []:
        if not isinstance(item, dict) or not item.get("cell"):
            continue
        worksheet[str(item["cell"])] = _coerce(item.get("value"))
        changed += 1
    if spec.get("formula") and spec.get("cell"):
        worksheet[str(spec["cell"])] = str(spec["formula"])
        changed += 1
    workbook.save(str(path))
    emit(True, f"Updated {changed} cell(s) in {name}", path=path, count=changed)


def add_chart(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Workbook not found: {path}")
    workbook = openpyxl.load_workbook(str(path))
    name = str(spec.get("sheet") or workbook.sheetnames[0])
    worksheet = workbook[name]
    chart_type = str(spec.get("chart_type") or "bar").lower()
    chart = {"bar": BAR, "line": LINE, "pie": PIE}.get(chart_type, BAR)()
    chart.title = str(spec.get("title") or f"{chart_type.title()} chart")
    data_range = str(spec.get("data_range") or "")
    if not data_range:
        fail("data_range is required, e.g. 'B2:B10'.")
    data = Reference(worksheet, range_string=f"{name}!{data_range}")
    chart.add_data(data, titles_from_data=bool(spec.get("titles_from_data")))
    categories = str(spec.get("categories_range") or "")
    if categories:
        chart.set_categories(Reference(worksheet, range_string=f"{name}!{categories}"))
    worksheet.add_chart(chart, str(spec.get("anchor") or "E2"))
    workbook.save(str(path))
    emit(True, f"Added {chart_type} chart to {name}", path=path)


def read(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"Workbook not found: {path}")
    workbook = openpyxl.load_workbook(str(path), data_only=True)
    name = str(spec.get("sheet") or workbook.sheetnames[0])
    worksheet = workbook[name]
    limit = int(spec.get("max_rows") or 100)
    lines = []
    for index, row in enumerate(worksheet.iter_rows(values_only=True)):
        if index >= limit:
            lines.append(f"[TRUNCATED after {limit} rows]")
            break
        lines.append("\t".join("" if value is None else str(value) for value in row))
    emit(True, clip(f"Sheet: {name}\n" + "\n".join(lines)))


def profile(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"Workbook not found: {path}")
    pandas = require_module("pandas", "pandas")
    name = str(spec.get("sheet") or 0)
    frame = pandas.read_excel(str(path), sheet_name=name)
    summary = {
        "rows": int(frame.shape[0]),
        "columns": int(frame.shape[1]),
        "column_names": [str(column) for column in frame.columns],
        "dtypes": {str(key): str(value) for key, value in frame.dtypes.items()},
        "missing": {str(key): int(value) for key, value in frame.isna().sum().items()},
    }
    emit(True, "Workbook profile ready.", data=summary)


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "create").strip().lower()
    handlers = {
        "create": create,
        "append_rows": append_rows,
        "append": append_rows,
        "set_cells": set_cells,
        "set": set_cells,
        "add_chart": add_chart,
        "chart": add_chart,
        "read": read,
        "profile": profile,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(
            f"Unknown xlsx operation '{operation}'. Use create, append_rows, "
            "set_cells, add_chart, read, or profile."
        )
    handler(spec)


if __name__ == "__main__":
    main()
