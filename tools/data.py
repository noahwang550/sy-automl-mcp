"""Dataset tools: load_dataset, validate_dataset, and a shared reader."""
from __future__ import annotations

import io
import urllib.request
from typing import Any

import pandas as pd

from config import (
    MAX_DATASET_COLUMNS,
    MAX_DATASET_MB,
    MAX_DATASET_ROWS,
    dataset_path,
    validate_id,
)
from serialization import sample_rows

from ._common import envelope_call

# Single-file dataset layout: artifacts/datasets/<dataset_id>/data.<ext>
_DATA_FILENAME = "data"


def _detect_format(fmt: str, content_or_name: str) -> str:
    if fmt and fmt != "auto":
        return fmt
    low = content_or_name.lower()
    if low.endswith(".parquet"):
        return "parquet"
    if low.endswith(".json"):
        return "json"
    return "csv"  # default


def _read_df(buf_or_path: Any, fmt: str) -> pd.DataFrame:
    if fmt == "parquet":
        return pd.read_parquet(buf_or_path)
    if fmt == "json":
        return pd.read_json(buf_or_path)
    return pd.read_csv(buf_or_path)  # csv default


def _write_df(df: pd.DataFrame, path: Any, fmt: str) -> None:
    if fmt == "parquet":
        df.to_parquet(path, index=False)
    elif fmt == "json":
        df.to_json(path, orient="records")
    else:
        df.to_csv(path, index=False)


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _is_inline(source: str) -> bool:
    """Heuristic: inline CSV/JSON has a newline (multi-row) or looks like JSON."""
    return "\n" in source or source.lstrip().startswith("{")


def _enforce_size_limits(df: pd.DataFrame) -> None:
    """Reject datasets that exceed configured row/column/memory limits."""
    rows, cols = df.shape
    if rows > MAX_DATASET_ROWS:
        raise ValueError(
            f"Dataset exceeds row limit: {rows} > {MAX_DATASET_ROWS}"
        )
    if cols > MAX_DATASET_COLUMNS:
        raise ValueError(
            f"Dataset exceeds column limit: {cols} > {MAX_DATASET_COLUMNS}"
        )
    # Rough memory estimate (pandas overhead is larger, but this caps abuse).
    mb = df.memory_usage(deep=True).sum() / (1024 * 1024)
    if mb > MAX_DATASET_MB:
        raise ValueError(
            f"Dataset exceeds memory limit: {mb:.1f}MB > {MAX_DATASET_MB}MB"
        )


def dataset_file(dataset_id: str, fmt: str) -> str:
    """Return the on-disk filename for a dataset of a given format."""
    ext = {"csv": "csv", "parquet": "parquet", "json": "json"}.get(fmt, "csv")
    return f"{_DATA_FILENAME}.{ext}"


def read_dataset_df(dataset_id: str) -> pd.DataFrame:
    """Read a previously loaded dataset back into a DataFrame."""
    validate_id(dataset_id, "dataset_id")
    ddir = dataset_path(dataset_id)
    if not ddir.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_id}")
    candidates = sorted(ddir.glob(f"{_DATA_FILENAME}.*"))
    if not candidates:
        raise FileNotFoundError(f"Dataset {dataset_id} has no data file")
    ext = candidates[0].suffix.lower().lstrip(".")
    return _read_df(candidates[0], ext if ext in {"csv", "parquet", "json"} else "csv")


def _load_dataset(
    source: str,
    dataset_id: str,
    format: str = "auto",
) -> dict[str, Any]:
    """Core implementation for load_dataset (returns a plain dict)."""
    validate_id(dataset_id, "dataset_id")
    fmt = _detect_format(format, source)

    ddir = dataset_path(dataset_id)
    ddir.mkdir(parents=True, exist_ok=True)
    out_file = ddir / dataset_file(dataset_id, fmt)

    if _is_url(source):
        urllib.request.urlretrieve(source, out_file)  # noqa: S310 (trusted local tool)
    elif _is_inline(source):
        out_file.write_text(source, encoding="utf-8")
    else:
        # Treat as a relative filename within the datasets volume.
        validate_id(source, "source_filename")
        src = (ddir.parent / source) if "/" not in source else None
        if src is None or not src.exists():
            # Allow a file already placed directly under this dataset_id dir.
            src = ddir / source
        if not src.exists():
            raise FileNotFoundError(
                f"source {source!r} not found in datasets volume; pass inline "
                "content or an http(s) URL, or place the file under artifacts/datasets/"
            )
        df = _read_df(src, fmt)
        _write_df(df, out_file, fmt)

    df = _read_df(out_file, fmt)
    _enforce_size_limits(df)
    return {
        "dataset_id": dataset_id,
        "path": str(out_file),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "sample": sample_rows(df, 5),
    }


def load_dataset(
    source: str,
    dataset_id: str,
    format: str = "auto",
) -> dict[str, Any]:
    """Import a dataset into artifacts/datasets and return an envelope.

    Args:
        source: One of:
            - inline CSV/JSON text (contains a newline or starts with ``{``),
            - an ``http(s)://`` URL to download,
            - a filename already present in the mounted ``artifacts/datasets/``
              volume (relative name only; absolute paths are rejected).
        dataset_id: Unique identifier for the dataset (letters/digits/_ . -).
        format: ``csv`` | ``parquet`` | ``json`` | ``auto``.

    Returns:
        Envelope with data containing rows, columns, dtypes, and a 5-row sample.
    """
    return envelope_call(_load_dataset, source, dataset_id, format)


def _validate_dataset(
    dataset_id: str,
    task_type: str | None = None,
    target: str | None = None,
    required_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Core implementation for validate_dataset."""
    df = read_dataset_df(dataset_id)
    columns = list(df.columns)
    issues: list[str] = []

    for col in required_columns or []:
        if col not in columns:
            issues.append(f"Missing required column: {col}")

    if target is not None:
        if target not in columns:
            issues.append(f"Target column not found: {target}")
        else:
            nunique = int(df[target].nunique(dropna=True))
            if task_type == "classification" and nunique < 2:
                issues.append(
                    f"Target {target} has {nunique} unique values; classification needs >=2"
                )

    missing = {c: int(df[c].isna().sum()) for c in columns if df[c].isna().any()}

    return {
        "dataset_id": dataset_id,
        "valid": len(issues) == 0,
        "issues": issues,
        "rows": int(len(df)),
        "columns": columns,
        "inferred_dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "missing_counts": missing,
        "recommended_action": "ok" if not issues else "fix issues before training",
    }


def validate_dataset(
    dataset_id: str,
    task_type: str | None = None,
    target: str | None = None,
    required_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a dataset before training. Reports missing values and type issues."""
    return envelope_call(_validate_dataset, dataset_id, task_type, target, required_columns)


def read_inline_or_dataset(
    dataset_id: str | None, inline_csv: str | None
) -> pd.DataFrame:
    """Resolve exactly one of (dataset_id, inline_csv) to a DataFrame."""
    if (dataset_id is None) == (inline_csv is None):
        raise ValueError("Provide exactly one of dataset_id or inline_csv")
    if dataset_id is not None:
        return read_dataset_df(dataset_id)
    return pd.read_csv(io.StringIO(inline_csv))  # type: ignore[arg-type]
