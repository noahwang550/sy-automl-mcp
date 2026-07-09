"""Reduce pandas / numpy / AutoGluon objects to JSON-serializable Python types.

AutoGluon tools return DataFrames, Series, and numpy scalars — none of which
serialize cleanly over MCP (NaN, Timestamps, np.int64, etc.). Every tool that
returns data to the LLM must route it through :func:`to_jsonable`.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd

try:  # numpy is an AutoGluon dependency but guard for unit-test minimalism
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


def to_jsonable_value(value: Any) -> Any:
    """Recursively convert a single value to a JSON-safe primitive."""
    # None / bool first (bool is a subclass of int)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if np is not None and isinstance(value, np.integer):
        return int(value)
    if np is not None and isinstance(value, np.floating):
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if np is not None and isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): to_jsonable_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable_value(v) for v in value]
    # pd.Series, np.ndarray, etc. -> list
    if hasattr(value, "tolist"):
        try:
            return [to_jsonable_value(v) for v in value.tolist()]
        except Exception:
            pass
    # Last resort: stringify
    return str(value)


def to_jsonable(df: Any, orient: str = "records") -> Any:
    """Convert a DataFrame (or any value) to a JSON-safe structure.

    For DataFrames, returns ``{"columns": [...], "rows": [...]}`` by default,
    or a list of row-dicts when ``orient="records"``.
    """
    if df is None:
        return None
    if isinstance(df, pd.DataFrame):
        if orient == "records":
            return [to_jsonable_value(r) for r in df.to_dict(orient="records")]
        return {
            "columns": list(df.columns),
            "rows": [to_jsonable_value(r) for r in df.to_dict(orient="records")],
        }
    if isinstance(df, pd.Series):
        return [to_jsonable_value(v) for v in df.tolist()]
    return to_jsonable_value(df)


def sample_rows(df: pd.DataFrame, n: int = 5) -> list[dict]:
    """Return the first ``n`` rows as JSON-safe dicts."""
    if df is None:
        return []
    head = df.head(n)
    return [to_jsonable_value(r) for r in head.to_dict(orient="records")]
