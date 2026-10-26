"""Profile a CSV and render a quick chart with pandas + matplotlib."""

from __future__ import annotations

from pathlib import Path

from winbrow.skill_script_utils import (
    clip, emit, ensure_parent, fail, load_spec, require_module, resolve_path,
)

pandas = require_module("pandas", "pandas")


def _read_csv(path: Path):
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return pandas.read_csv(path, encoding=encoding)
        except Exception as exc:
            last_error = exc
    fail(f"Could not read CSV: {last_error}")


def _profile(frame) -> dict:
    numeric = frame.select_dtypes(include="number")
    summary = {
        "rows": int(frame.shape[0]),
        "columns": int(frame.shape[1]),
        "column_names": [str(column) for column in frame.columns],
        "dtypes": {str(key): str(value) for key, value in frame.dtypes.items()},
        "missing": {str(key): int(value) for key, value in frame.isna().sum().items()},
        "numeric_summary": {},
        "top_values": {},
    }
    if not numeric.empty:
        described = numeric.describe().round(3).to_dict()
        summary["numeric_summary"] = {
            str(key): {str(k): float(v) for k, v in values.items()}
            for key, values in described.items()
        }
    for column in frame.columns[:8]:
        if frame[column].dtype == object:
            counts = frame[column].value_counts().head(5)
            summary["top_values"][str(column)] = {
                str(key): int(value) for key, value in counts.items()
            }
    return summary


def profile(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"CSV not found: {path}")
    frame = _read_csv(path)
    summary = _profile(frame)
    emit(True, "CSV profile ready.", data=summary)


def chart(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"CSV not found: {path}")
    matplotlib = require_module("matplotlib", "matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frame = _read_csv(path)
    x_column = str(spec.get("x") or frame.columns[0])
    y_column = str(spec.get("y") or "")
    if y_column not in frame.columns:
        numeric = list(frame.select_dtypes(include="number").columns)
        y_column = str(numeric[0]) if numeric else ""
    if x_column not in frame.columns or not y_column:
        fail(f"Unknown chart columns. Available: {list(frame.columns)}")

    chart_type = str(spec.get("chart_type") or "bar").lower()
    figure, axis = plt.subplots(figsize=(10, 5.5))
    if chart_type == "line":
        axis.plot(frame[x_column].astype(str), frame[y_column])
    elif chart_type == "pie":
        axis.pie(frame[y_column], labels=frame[x_column].astype(str), autopct="%1.0f%%")
    else:
        axis.bar(frame[x_column].astype(str), frame[y_column])
    axis.set_title(str(spec.get("title") or f"{y_column} by {x_column}"))
    axis.set_xlabel(x_column)
    if chart_type != "pie":
        axis.set_ylabel(y_column)
    axis.tick_params(axis="x", rotation=45)
    figure.tight_layout()

    output = resolve_path(
        spec.get("output_path")
        or (path.parent / f"{path.stem}_{chart_type}.png")
    )
    if output.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        output = output.with_suffix(".png")
    ensure_parent(output)
    figure.savefig(str(output), dpi=int(spec.get("dpi") or 150))
    plt.close(figure)
    emit(True, f"Saved chart: {output}", path=output)


def preview(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"CSV not found: {path}")
    frame = _read_csv(path)
    emit(True, clip(frame.head(int(spec.get("rows") or 20)).to_csv(index=False)))


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "profile").strip().lower()
    handlers = {
        "profile": profile,
        "summarize": profile,
        "chart": chart,
        "plot": chart,
        "preview": preview,
        "read": preview,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(f"Unknown CSV operation '{operation}'. Use profile, chart, or preview.")
    handler(spec)


if __name__ == "__main__":
    main()
