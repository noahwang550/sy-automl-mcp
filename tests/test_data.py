"""Tests for tools.data (no AutoGluon required)."""
from __future__ import annotations

import pytest

from tools.data import load_dataset, read_dataset_df, validate_dataset


def _data(result):
    """Extract the data payload from an tool envelope."""
    assert result["success"] is True, result.get("error")
    return result["data"]


def test_load_dataset_inline_csv(isolated_artifacts, iris_csv):
    result = load_dataset(source=iris_csv, dataset_id="iris", format="auto")
    summary = _data(result)
    assert summary["dataset_id"] == "iris"
    assert summary["rows"] == 4
    assert summary["columns"] == ["sepal_length", "sepal_width", "species"]
    assert len(summary["sample"]) == 4
    assert summary["sample"][0]["sepal_length"] == 5.1


def test_read_dataset_df_roundtrip(isolated_artifacts, iris_csv):
    load_dataset(source=iris_csv, dataset_id="iris")
    df = read_dataset_df("iris")
    assert list(df.columns) == ["sepal_length", "sepal_width", "species"]
    assert len(df) == 4


def test_validate_dataset_missing_target(isolated_artifacts, iris_csv):
    load_dataset(source=iris_csv, dataset_id="iris")
    res = validate_dataset(dataset_id="iris", task_type="classification", target="nope")
    data = _data(res)
    assert data["valid"] is False
    assert any("Target column not found" in i for i in data["issues"])


def test_validate_dataset_ok(isolated_artifacts, iris_csv):
    load_dataset(source=iris_csv, dataset_id="iris")
    res = validate_dataset(dataset_id="iris", target="species")
    data = _data(res)
    assert data["valid"] is True
    assert data["issues"] == []


def test_load_dataset_rejects_bad_id(isolated_artifacts, iris_csv):
    result = load_dataset(source=iris_csv, dataset_id="../escape")
    assert result["success"] is False
    assert "Invalid dataset_id" in result["error"]


def test_load_dataset_rejects_absolute_source(isolated_artifacts):
    result = load_dataset(source="/etc/passwd", dataset_id="x")
    assert result["success"] is False


def test_read_inline_or_dataset_requires_exactly_one(isolated_artifacts, iris_csv):
    from tools.data import read_inline_or_dataset

    load_dataset(source=iris_csv, dataset_id="iris")
    with pytest.raises(ValueError):
        read_inline_or_dataset(None, None)
    with pytest.raises(ValueError):
        read_inline_or_dataset("iris", "a,b\n1,2\n")
    df = read_inline_or_dataset("iris", None)
    assert len(df) == 4


import tools.data as data_module


def test_load_dataset_enforces_size_limit(monkeypatch, isolated_artifacts, iris_csv):
    """A dataset with more rows than MAX_DATASET_ROWS is rejected gracefully."""
    monkeypatch.setattr(data_module, "MAX_DATASET_ROWS", 2)
    result = load_dataset(source=iris_csv, dataset_id="iris")
    assert result["success"] is False
    assert "row limit" in result["error"].lower()
