"""Extract invoice metadata from PDFs and file them consistently."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, require_module, resolve_path

fitz = require_module("fitz", "PyMuPDF")

_DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b"),
    re.compile(r"\b(0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])[-/](20\d{2})\b"),
]
_AMOUNT = re.compile(
    r"(?:total|amount due|balance due|grand total)[^\d]{0,20}"
    r"([$€£]?\s?\d[\d,]*\.?\d{0,2})",
    re.I,
)
_INVOICE_NO = re.compile(
    r"(?:invoice|inv)[\s#:.-]*([A-Z0-9][A-Z0-9-]{2,})", re.I,
)
_VENDOR_HINT = re.compile(r"^(?:from|vendor|seller|supplier)\s*[:\-]\s*(.+)$", re.I)


def _pdf_text(path: Path) -> str:
    try:
        with fitz.open(str(path)) as document:
            return "\n".join(page.get_text() for page in document)
    except Exception:
        return ""


def _extract(path: Path) -> dict:
    text = _pdf_text(path) if path.suffix.lower() == ".pdf" else ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    date = ""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            date = match.group(0).replace("/", "-")
            break
    amount = ""
    match = _AMOUNT.search(text)
    if match:
        amount = match.group(1).replace(" ", "").replace(",", "")
    invoice_no = ""
    match = _INVOICE_NO.search(text)
    if match:
        invoice_no = match.group(1).strip("-")
    vendor = ""
    for line in lines[:12]:
        hint = _VENDOR_HINT.match(line)
        if hint:
            vendor = hint.group(1).strip()
            break
    if not vendor and lines:
        vendor = lines[0][:80]
    return {
        "file": str(path),
        "vendor": vendor,
        "date": date,
        "amount": amount,
        "invoice_no": invoice_no,
        "chars": len(text),
    }


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "", value or "").strip().replace(" ", "_")[:80]


def scan(spec: dict) -> None:
    folder = resolve_path(spec.get("path") or spec.get("input_dir") or "")
    if not folder.is_dir():
        fail(f"Folder not found: {folder}")
    records = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            records.append(_extract(path))
    if not records:
        fail("No invoice-like files found (PDF or image).")
    emit(True, f"Scanned {len(records)} file(s).", data={"records": records})


def organize(spec: dict) -> None:
    folder = resolve_path(spec.get("path") or spec.get("input_dir") or "")
    if not folder.is_dir():
        fail(f"Folder not found: {folder}")
    target = resolve_path(spec.get("target") or folder / "Invoices")
    dry_run = bool(spec.get("dry_run", True))
    records = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            continue
        info = _extract(path)
        year = "unknown-year"
        month = "unknown-month"
        if info["date"]:
            parts = info["date"].replace("/", "-").split("-")
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    year, month = parts[0], parts[1].zfill(2)
                else:
                    year, month = parts[2], parts[0].zfill(2)
        vendor = _safe(info["vendor"]) or "unknown-vendor"
        amount = _safe(info["amount"]).lstrip("$€£") or "amount-unknown"
        name = f"{info['date'] or 'undated'} {vendor} - Invoice - {amount}{path.suffix.lower()}"
        destination = target / year / month / name
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        info["destination"] = str(destination)
        records.append(info)
    if not records:
        fail("No invoice-like files found (PDF or image).")
    index = target / "invoice-index.csv"
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        with index.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "file", "vendor", "date", "amount", "invoice_no", "chars", "destination",
            ])
            writer.writeheader()
            writer.writerows(records)
    prefix = "Would organize" if dry_run else "Organized"
    emit(True, f"{prefix} {len(records)} invoice(s).", data={
        "dry_run": dry_run, "records": records, "index": str(index),
    })


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "scan").lower()
    handlers = {"scan": scan, "organize": organize, "apply": organize}
    handler = handlers.get(operation)
    if handler is None:
        fail(f"Unknown invoice operation '{operation}'. Use scan or organize.")
    handler(spec)


if __name__ == "__main__":
    main()
