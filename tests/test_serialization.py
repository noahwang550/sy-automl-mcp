"""Tests for serialization.to_jsonable / to_jsonable_value."""
from __future__ import annotations

import math
from datetime import datetime

import pandas as pd
import pytest

from serialization import to_jsonable, to_jsonable_value

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore


def test_none_and_bool_pass_through():
    assert to_jsonable_value(None) is None
    assert to_jsonable_value(True) is True
    assert to_jsonable_value(False) is False


def test_nan_and_inf_become_none():
    assert to_jsonable_value(float("nan")) is None
    assert to_jsonable_value(float("inf")) is None
    assert to_jsonable_value(float("-inf")) is None
    assert to_jsonable_value(3.14) == 3.14


def test_datetime_to_iso():
    dt = datetime(2026, 1, 2, 3, 4, 5)
    assert to_jsonable_value(dt) == "2026-01-02T03:04:05"


def test_numpy_scalars():
    if np is None:
        pytest.skip("numpy not installed")
    assert to_jsonable_value(np.int64(7)) == 7
    assert to_jsonable_value(np.float64(2.5)) == 2.5
    assert to_jsonable_value(np.float64("nan")) is None
    assert to_jsonable_value(np.bool_(True)) is True


def test_dataframe_records():
    df = pd.DataFrame({"a": [1, 2], "b": [None, 3.5]})
    out = to_jsonable(df, orient="records")
    assert out == [{"a": 1, "b": None}, {"a": 2, "b": 3.5}]


def test_dataframe_columns_and_rows():
    df = pd.DataFrame({"a": [1], "b": [math.nan]})
    out = to_jsonable(df, orient="list")
    assert out["columns"] == ["a", "b"]
    assert out["rows"] == [{"a": 1, "b": None}]


def test_nested_dict_and_list():
    assert to_jsonable_value({"a": [1, float("nan")], "b": (2, 3)}) == {
        "a": [1, None],
        "b": [2, 3],
    }


def test_series_to_list():
    s = pd.Series([1, 2, 3])
    assert to_jsonable(s) == [1, 2, 3]
