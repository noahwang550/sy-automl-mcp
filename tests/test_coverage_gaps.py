"""Targeted coverage tests for low-coverage pure-logic branches.

These exercise branches of config / serialization / tools.data /
tools.model_management / tasks.manager that the AutoGluon-driven e2e tests do
not reach (error paths, format branches, registry edge cases). None require
AutoGluon, so they run on the tabular tier too.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import pytest

import config
import tools.data as data_mod
from serialization.dataframe import sample_rows, to_jsonable, to_jsonable_value
from tasks import get_task_manager
from tasks.manager import SUCCESS, shutdown
from tools.data import (
    _detect_format,
    _read_df,
    _write_df,
    read_dataset_df,
    read_inline_or_dataset,
)
from tools.model_management import _list_models, _load_predictor_obj, list_models, load_model

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


def _ok(result):
    assert result["success"] is True, result.get("error")
    return result["data"]


# ---------------------------------------------------------------------------
# config.py — validate_id reserved names, directory_size_bytes on a file,
# corrupt registry, get_registry_entry miss.
# ---------------------------------------------------------------------------


def test_validate_id_rejects_reserved_dotdot(isolated_artifacts):
    with pytest.raises(ValueError):
        config.validate_id("..", "model_id")
    with pytest.raises(ValueError):
        config.validate_id(".", "dataset_id")


def test_directory_size_bytes_on_single_file(isolated_artifacts, tmp_path):
    f = tmp_path / "one.bin"
    f.write_bytes(b"x" * 512)
    assert config.directory_size_bytes(f) == 512


def test_load_registry_tolerates_corrupt_json(isolated_artifacts):
    # Write garbage to the registry path; reads must degrade to [] not raise.
    config.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.REGISTRY_PATH.write_text("{not valid json", encoding="utf-8")
    assert config.list_registry_models() == []


def test_get_registry_entry_missing_returns_none(isolated_artifacts):
    assert config.get_registry_entry("absent_model") is None


# ---------------------------------------------------------------------------
# serialization/dataframe.py — numpy / Timestamp / bytes / set / ndarray
# branches, non-records DataFrame orient, sample_rows(None).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(np is None, reason="numpy required")
def test_to_jsonable_value_handles_numpy_and_specials():
    assert to_jsonable_value(np.int64(7)) == 7
    assert to_jsonable_value(np.float64(3.5)) == 3.5
    assert to_jsonable_value(np.float64("nan")) is None
    assert to_jsonable_value(np.bool_(True)) is True
    assert to_jsonable_value(np.array([1, 2, 3])) == [1, 2, 3]
    assert to_jsonable_value({"a": np.int64(1)}) == {"a": 1}
    assert to_jsonable_value({1, 2, 3}) == [1, 2, 3]
    assert to_jsonable_value(b"hi") == "hi"


def test_to_jsonable_value_handles_datetime_and_timestamp():
    import datetime as dt

    assert to_jsonable_value(dt.date(2026, 7, 9)).startswith("2026-07-09")
    assert to_jsonable_value(dt.datetime(2026, 7, 9, 1, 2, 3)).startswith("2026-07-09T01:02:03")
    assert to_jsonable_value(pd.Timestamp("2026-07-09")).startswith("2026-07-09")


def test_to_jsonable_dataframe_non_records_orient():
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    out = to_jsonable(df, orient="columns")
    assert out["columns"] == ["a", "b"]
    assert out["rows"][0]["a"] == 1


def test_to_jsonable_series_and_none():
    assert to_jsonable(None) is None
    s = pd.Series([1, 2, 3])
    assert to_jsonable(s) == [1, 2, 3]


def test_sample_rows_none_returns_empty():
    assert sample_rows(None) == []


# ---------------------------------------------------------------------------
# tools/data.py — format detection, read/write round-trips, size limits,
# read_dataset_df errors, file-source load, validate_dataset edge cases,
# read_inline_or_dataset contract.
# ---------------------------------------------------------------------------


def test_detect_format_branches():
    assert _detect_format("parquet", "x") == "parquet"
    assert _detect_format("auto", "a.parquet") == "parquet"
    assert _detect_format("auto", "a.json") == "json"
    assert _detect_format("auto", "a.csv") == "csv"
    assert _detect_format("auto", "no_ext") == "csv"


def test_write_then_read_json_and_csv(tmp_path):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    jp = tmp_path / "d.json"
    _write_df(df, jp, "json")
    assert _read_df(jp, "json").shape == (2, 2)
    cp = tmp_path / "d.csv"
    _write_df(df, cp, "csv")
    assert _read_df(cp, "csv").shape == (2, 2)


def test_write_then_read_parquet(tmp_path):
    pyarrow = pytest.importorskip("pyarrow")
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    pp = tmp_path / "d.parquet"
    _write_df(df, pp, "parquet")
    assert _read_df(pp, "parquet").shape == (2, 2)


def test_enforce_size_limits_rejects_too_many_columns(isolated_artifacts, monkeypatch):
    monkeypatch.setattr(data_mod, "MAX_DATASET_COLUMNS", 0)
    res = data_mod.load_dataset("a,b\n1,2\n3,4\n", "wide_ds")
    assert res["success"] is False
    assert "column limit" in res["error"]


def test_enforce_size_limits_rejects_oversize_memory(isolated_artifacts, monkeypatch):
    monkeypatch.setattr(data_mod, "MAX_DATASET_MB", 0)
    res = data_mod.load_dataset("a\n1\n", "big_ds")
    assert res["success"] is False
    assert "memory limit" in res["error"]


def test_read_dataset_df_missing_dir_and_missing_data_file(isolated_artifacts):
    with pytest.raises(FileNotFoundError):
        read_dataset_df("never_loaded")
    # Empty dataset dir with no data file.
    ddir = config.dataset_path("empty_ds")
    ddir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        read_dataset_df("empty_ds")


def test_load_dataset_from_volume_file_source(isolated_artifacts):
    # Place a CSV directly in the datasets volume and load it by filename.
    src = config.DATASETS_DIR / "external.csv"
    config.DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    src.write_text("c1,c2\n1,2\n3,4\n", encoding="utf-8")
    res = data_mod.load_dataset("external.csv", "copied_ds")
    data = _ok(res)
    assert data["rows"] == 2
    assert data["columns"] == ["c1", "c2"]


def test_load_dataset_unknown_file_source_errors(isolated_artifacts):
    res = data_mod.load_dataset("nope.csv", "bad_source")
    assert res["success"] is False
    assert "not found" in res["error"].lower()


def test_validate_dataset_missing_required_and_single_class(isolated_artifacts):
    _ok(data_mod.load_dataset("label,v\na,1\na,2\n", "vc"))
    res = data_mod.validate_dataset("vc", task_type="classification", target="label")
    data = _ok(res)
    assert any("classification needs" in i for i in data["issues"])
    res2 = data_mod.validate_dataset("vc", required_columns=["missing_col"])
    data2 = _ok(res2)
    assert any("Missing required column" in i for i in data2["issues"])


def test_read_inline_or_dataset_requires_exactly_one(isolated_artifacts):
    with pytest.raises(ValueError):
        read_inline_or_dataset(None, None)
    with pytest.raises(ValueError):
        read_inline_or_dataset("d", "a,b\n1,2\n")
    _ok(data_mod.load_dataset("a,b\n1,2\n", "rid"))
    assert read_inline_or_dataset("rid", None).shape == (1, 2)
    assert read_inline_or_dataset(None, "a,b\n1,2\n").shape == (1, 2)


# ---------------------------------------------------------------------------
# tools/model_management.py — list filters, unsupported type, load miss,
# delete with real dir (freed bytes).
# ---------------------------------------------------------------------------


def _register(model_id: str, created_at, mtype: str = "tabular") -> None:
    config.register_model(
        {
            "model_id": model_id,
            "type": mtype,
            "path": str(config.model_path(model_id)),
            "target": "y",
            "problem_type": "auto",
            "created_at": created_at,
            "task_id": "t",
            "size_mb": 0.0,
        }
    )


def test_list_models_created_after_and_before_filters(isolated_artifacts):
    _register("m_jan", "2026-01-01")
    _register("m_feb", "2026-02-01")
    _register("m_mar", "2026-03-01")

    after = _ok(list_models(created_after="2026-01-15"))
    assert {m["model_id"] for m in after["models"]} == {"m_feb", "m_mar"}

    before = _ok(list_models(created_before="2026-02-15"))
    assert {m["model_id"] for m in before["models"]} == {"m_jan", "m_feb"}


def test_load_predictor_obj_unsupported_type_raises(isolated_artifacts):
    with pytest.raises(ValueError, match="Unsupported model type"):
        _load_predictor_obj({"type": "bogus", "path": "/nope"})


def test_load_model_missing_returns_failure_envelope(isolated_artifacts):
    res = load_model("absent_model")
    assert res["success"] is False
    assert "not found" in res["error"].lower()


def test_delete_model_with_real_dir_reports_freed(isolated_artifacts):
    _register("real_m", time.time())
    mdir = config.model_path("real_m")
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "weights.bin").write_bytes(b"x" * 100_000)
    from tools.model_management import delete_model

    res = delete_model("real_m", confirm=True)
    data = _ok(res)
    assert data["deleted"] is True
    assert data["freed_mb"] > 0
    assert not mdir.exists()


# ---------------------------------------------------------------------------
# tasks/manager.py — list(), shutdown(), and the new progress field.
# ---------------------------------------------------------------------------


def test_manager_list_returns_dicts(isolated_artifacts):
    mgr = get_task_manager()

    def job(task):
        return {"ok": True}

    task_id = mgr.submit("ljob", job, {})
    for _ in range(100):
        if mgr.status(task_id)["status"] == SUCCESS:
            break
        time.sleep(0.05)
    lst = mgr.list()
    assert isinstance(lst, list)
    assert any(t["task_id"] == task_id for t in lst)


def test_status_includes_progress_field(isolated_artifacts):
    mgr = get_task_manager()

    def job(task):
        return {"ok": True}

    task_id = mgr.submit("pjob", job, {})
    for _ in range(100):
        if mgr.status(task_id)["status"] == SUCCESS:
            break
        time.sleep(0.05)
    st = mgr.status(task_id)
    assert "progress" in st
    assert st["progress"]["available"] is True
    assert st["progress"]["status"] == SUCCESS


def test_shutdown_clears_singleton(isolated_artifacts):
    mgr = get_task_manager()
    assert mgr is get_task_manager()  # same singleton
    shutdown()
    import tasks.manager as manager_mod

    assert manager_mod._SINGLETON is None
