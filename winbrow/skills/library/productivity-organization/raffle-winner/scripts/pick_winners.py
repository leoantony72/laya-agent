"""Pick cryptographically random raffle winners from a list, CSV, or XLSX."""

from __future__ import annotations

import csv
import random
import secrets
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path


def _rows_from_csv(path: Path) -> list[list[str]]:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with path.open("r", newline="", encoding=encoding) as handle:
                return [row for row in csv.reader(handle)]
        except Exception:
            continue
    fail(f"Could not read CSV: {path}")
    return []


def _rows_from_xlsx(path: Path) -> list[list[str]]:
    openpyxl = __import__("openpyxl")
    workbook = openpyxl.load_workbook(str(path), data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    return [[("" if value is None else str(value)) for value in row] for row in sheet.iter_rows(values_only=True)]


def _load_entries(spec: dict) -> list[dict]:
    entries = spec.get("entries")
    if isinstance(entries, list):
        return [{"name": str(item), "raw": str(item)} for item in entries]
    path_value = spec.get("path") or spec.get("input_path")
    if not path_value:
        fail("Provide entries or a CSV/XLSX path.")
    path = resolve_path(path_value)
    if not path.is_file():
        fail(f"File not found: {path}")
    if path.suffix.lower() == ".xlsx":
        rows = _rows_from_xlsx(path)
    else:
        rows = _rows_from_csv(path)
    if not rows:
        fail("The file is empty.")
    header = [str(value).strip().lower() for value in rows[0]]
    name_index = 0
    for candidate in ("name", "email", "entry", "participant", "winner"):
        if candidate in header:
            name_index = header.index(candidate)
            break
    has_header = bool(spec.get("has_header", True))
    data = rows[1:] if has_header else rows
    result = []
    for row in data:
        if not row or not any(str(value).strip() for value in row):
            continue
        name = str(row[name_index]).strip() if name_index < len(row) else str(row[0]).strip()
        result.append({"name": name, "raw": ",".join(str(value) for value in row)})
    return result


def pick(spec: dict) -> None:
    entries = _load_entries(spec)
    if not entries:
        fail("No entries found.")
    count = max(1, int(spec.get("count") or 1))
    if count > len(entries) and bool(spec.get("unique", True)):
        fail(f"Cannot pick {count} unique winners from {len(entries)} entries.")
    seed = spec.get("seed")
    rng = random.Random(seed) if seed not in (None, "") else secrets.SystemRandom()
    if bool(spec.get("unique", True)):
        winners = rng.sample(entries, count)
    else:
        winners = [rng.choice(entries) for _ in range(count)]

    output = spec.get("output_path")
    if output:
        target = resolve_path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["winner", "entry"])
            for winner in winners:
                writer.writerow([winner["name"], winner["raw"]])
    emit(True, "Winners: " + ", ".join(winner["name"] for winner in winners), data={
        "winners": winners,
        "entry_count": len(entries),
        "seed_used": seed not in (None, ""),
        "output_path": str(output) if output else None,
    })


def main() -> None:
    pick(load_spec())


if __name__ == "__main__":
    main()
