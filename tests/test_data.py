"""Tests for tools.data (no AutoGluon required)."""
from __future__ import annotations

import base64

import pytest

from tools.data import (
    finalize_dataset,
    load_dataset,
    read_dataset_df,
    upload_dataset,
    upload_dataset_chunk,
    validate_dataset,
)


def _data(result):
    """Extract the data payload from an tool envelope."""
    assert result["success"] is True, result.get("error")
    return result["data"]


def _err(result):
    assert result["success"] is False
    return result["error"]


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


# ---------------------------------------------------------------------------
# Chunked upload (upload_dataset_chunk + finalize_dataset)
# ---------------------------------------------------------------------------

def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_upload_dataset_single_shot(isolated_artifacts, iris_csv):
    """upload_dataset accepts a base64 blob and produces the same envelope as load_dataset."""
    b64 = base64.b64encode(iris_csv.encode("utf-8")).decode("ascii")
    summary = _data(upload_dataset(dataset_id="iris", content_base64=b64, format="csv"))
    assert summary["rows"] == 4
    assert summary["columns"] == ["sepal_length", "sepal_width", "species"]
    df = read_dataset_df("iris")
    assert len(df) == 4


def test_upload_dataset_rejects_bad_base64(isolated_artifacts):
    result = upload_dataset(dataset_id="x", content_base64="!!! not base64 !!!", format="csv")
    assert result["success"] is False
    assert "base64" in result["error"].lower()


def test_upload_dataset_rejects_oversized(monkeypatch, isolated_artifacts, iris_csv):
    monkeypatch.setattr(data_module, "MAX_UPLOAD_CHUNK_BYTES", 8)
    b64 = base64.b64encode(iris_csv.encode("utf-8")).decode("ascii")
    result = upload_dataset(dataset_id="x", content_base64=b64, format="csv")
    assert result["success"] is False
    assert "MAX_UPLOAD_CHUNK_BYTES" in result["error"]
    # Error message must point the caller at the chunked fallback.
    assert "upload_dataset_chunk" in result["error"]


def test_upload_dataset_rejects_bad_id(isolated_artifacts, iris_csv):
    b64 = base64.b64encode(iris_csv.encode("utf-8")).decode("ascii")
    result = upload_dataset(dataset_id="../escape", content_base64=b64, format="csv")
    assert result["success"] is False
    assert "Invalid dataset_id" in result["error"]


def test_upload_dataset_strips_data_uri_prefix(isolated_artifacts, iris_csv):
    """Agent callers often wrap base64 as a data URI; the decoder strips it."""
    raw_b64 = base64.b64encode(iris_csv.encode("utf-8")).decode("ascii")
    data_uri = f"data:text/csv;base64,{raw_b64}"
    summary = _data(upload_dataset(dataset_id="iris", content_base64=data_uri, format="csv"))
    assert summary["rows"] == 4


def test_upload_dataset_tolerates_wrapped_base64(isolated_artifacts, iris_csv):
    """Line-wrapped base64 (76-col, common from encoders) decodes correctly."""
    raw_b64 = base64.b64encode(iris_csv.encode("utf-8")).decode("ascii")
    wrapped = "\n".join(raw_b64[i:i+76] for i in range(0, len(raw_b64), 76))
    summary = _data(upload_dataset(dataset_id="iris", content_base64=wrapped, format="csv"))
    assert summary["rows"] == 4


def test_upload_chunk_strips_data_uri_prefix(isolated_artifacts, iris_csv):
    raw_b64 = base64.b64encode(iris_csv.encode("utf-8")).decode("ascii")
    data_uri = f"data:text/csv;base64,{raw_b64}"
    r = upload_dataset_chunk(
        dataset_id="iris",
        chunk_index=0,
        total_chunks=1,
        content_base64=data_uri,
        format="csv",
    )
    assert r["success"] is True
    assert _data(r)["bytes_received"] == len(iris_csv)
    summary = _data(finalize_dataset(dataset_id="iris", format="csv"))
    assert summary["rows"] == 4


def test_upload_chunked_csv_roundtrip(isolated_artifacts, iris_csv):
    # Split the inline CSV into 2 chunks uploaded out of order.
    half = len(iris_csv) // 2
    cut = iris_csv.index("\n", half) + 1  # don't split mid-row
    c0, c1 = iris_csv[:cut], iris_csv[cut:]

    r1 = upload_dataset_chunk(
        dataset_id="iris",
        chunk_index=1,
        total_chunks=2,
        content_base64=_b64(c1),
        format="csv",
    )
    r0 = upload_dataset_chunk(
        dataset_id="iris",
        chunk_index=0,
        total_chunks=2,
        content_base64=_b64(c0),
        format="csv",
    )
    assert _data(r1)["received_indices"] == [1]
    assert _data(r0)["received_indices"] == [0, 1]

    summary = _data(finalize_dataset(dataset_id="iris", format="csv"))
    assert summary["rows"] == 4
    assert summary["columns"] == ["sepal_length", "sepal_width", "species"]

    df = read_dataset_df("iris")
    assert list(df.columns) == ["sepal_length", "sepal_width", "species"]
    assert len(df) == 4

    # .chunks/ must be cleaned up after a successful finalize.
    assert not (isolated_artifacts / "datasets" / "iris" / ".chunks").exists()


def test_finalize_rejects_missing_chunk(isolated_artifacts, iris_csv):
    upload_dataset_chunk(
        dataset_id="iris",
        chunk_index=0,
        total_chunks=3,
        content_base64=_b64(iris_csv),
        format="csv",
    )
    result = finalize_dataset(dataset_id="iris", format="csv")
    assert result["success"] is False
    assert "missing" in result["error"].lower()
    # Chunks remain intact so the caller can upload the missing ones.
    assert (isolated_artifacts / "datasets" / "iris" / ".chunks").exists()


def test_upload_chunk_rejects_bad_id(isolated_artifacts):
    result = upload_dataset_chunk(
        dataset_id="../escape",
        chunk_index=0,
        total_chunks=1,
        content_base64=_b64("a,b\n1,2\n"),
    )
    assert result["success"] is False
    assert "Invalid dataset_id" in result["error"]


def test_upload_chunk_rejects_bad_base64(isolated_artifacts):
    result = upload_dataset_chunk(
        dataset_id="x",
        chunk_index=0,
        total_chunks=1,
        content_base64="!!! not base64 !!!",
        format="csv",
    )
    assert result["success"] is False
    assert "base64" in result["error"].lower()


def test_upload_chunk_rejects_oversized(monkeypatch, isolated_artifacts):
    monkeypatch.setattr(data_module, "MAX_UPLOAD_CHUNK_BYTES", 8)
    result = upload_dataset_chunk(
        dataset_id="x",
        chunk_index=0,
        total_chunks=1,
        content_base64=_b64("a" * 1024),
        format="csv",
    )
    assert result["success"] is False
    assert "MAX_UPLOAD_CHUNK_BYTES" in result["error"]


def test_upload_chunk_rejects_index_out_of_range(isolated_artifacts):
    result = upload_dataset_chunk(
        dataset_id="x",
        chunk_index=5,
        total_chunks=2,
        content_base64=_b64("a,b\n1,2\n"),
        format="csv",
    )
    assert result["success"] is False
    assert "out of range" in result["error"]


def test_upload_chunk_total_chunks_mismatch(isolated_artifacts, iris_csv):
    upload_dataset_chunk(
        dataset_id="x",
        chunk_index=0,
        total_chunks=2,
        content_base64=_b64(iris_csv),
        format="csv",
    )
    result = upload_dataset_chunk(
        dataset_id="x",
        chunk_index=1,
        total_chunks=3,  # different from the previously declared 2
        content_base64=_b64(iris_csv),
        format="csv",
    )
    assert result["success"] is False
    assert "mismatch" in result["error"].lower()


def test_finalize_without_any_upload_fails(isolated_artifacts):
    result = finalize_dataset(dataset_id="never", format="csv")
    assert result["success"] is False
    assert "no upload in progress" in result["error"].lower()


def test_finalize_enforces_size_limit(monkeypatch, isolated_artifacts):
    # A 3-row CSV that exceeds a tightened row limit of 2.
    csv = "a,b\n1,2\n3,4\n5,6\n"
    upload_dataset_chunk(
        dataset_id="x",
        chunk_index=0,
        total_chunks=1,
        content_base64=_b64(csv),
        format="csv",
    )
    monkeypatch.setattr(data_module, "MAX_DATASET_ROWS", 2)
    result = finalize_dataset(dataset_id="x", format="csv")
    assert result["success"] is False
    assert "row limit" in result["error"].lower()
    # On size-limit failure the partial file and chunks are removed.
    assert not (isolated_artifacts / "datasets" / "x" / "data.csv").exists()
    assert not (isolated_artifacts / "datasets" / "x" / ".chunks").exists()
