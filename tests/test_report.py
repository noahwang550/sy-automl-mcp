"""Unit tests for tools/report.py — assembly, persistence, FI, leakage."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

import config
from config import configure, model_path
from serialization import to_jsonable
from tasks.manager import Task


@pytest.fixture
def isolated_artifacts(tmp_path, monkeypatch):
    configure(tmp_path)
    yield tmp_path


@pytest.fixture
def stub_predictor():
    """Predictor stub matching AutoGluon 1.6.1 API surface (verified via inspect)."""
    p = MagicMock()
    p.problem_type = "multiclass"
    em = MagicMock()
    em.name = "accuracy"
    em.greater_is_better = True
    p.eval_metric = em
    p.model_best = "LightGBM"
    lb = pd.DataFrame({
        "model": ["LightGBM", "RandomForest", "XGBoost"],
        "score_val": [0.96, 0.92, 0.91],
        "score_test": [0.95, 0.91, 0.90],
        "fit_time": [10.0, 20.0, 15.0],
        "pred_time": [0.05, 0.10, 0.08],
    })
    p.leaderboard.return_value = lb
    p.fit_summary.return_value = {
        "model_types": {"LightGBM": "LGBModel"},
        "model_performance": {"LightGBM": 0.94, "RandomForest": 0.91},
    }
    fi = pd.DataFrame({
        "feature": ["is_seed", "age", "income"],
        "importance": [0.95, 0.18, 0.05],
        "stddev": [0.01, 0.02, 0.01],
    })
    p.feature_importance.return_value = fi
    return p


def _make_task(model_id="m01", dataset_id="d01"):
    return Task(task_id="t_test", type="train_tabular",
                params={"model_id": model_id, "dataset_id": dataset_id,
                        "target": "segment", "time_limit": 600})


def test_assemble_report_tabular_schema(stub_predictor, isolated_artifacts):
    from tools import report
    task = _make_task()
    # Need a model dir for relative paths.
    model_path("m01").mkdir(parents=True, exist_ok=True)
    data_summary = {"target": "segment", "target_classes": ["A", "B"],
                    "rows_trained": 800, "problem_type": "multiclass"}
    rep = report.assemble_report(
        predictor_type="tabular", task=task, params=task.params,
        data_summary=data_summary, training_config_extra={},
        predictor=stub_predictor, leaderboard=stub_predictor.leaderboard(),
        fit_summary=stub_predictor.fit_summary(), duration_seconds=12.3)
    assert rep["schema_version"] == 1
    assert rep["predictor_type"] == "tabular"
    assert rep["model_id"] == "m01"
    assert rep["task_id"] == "t_test"
    assert rep["status"] == "success"
    assert rep["duration_seconds"] == 12.3
    assert rep["evaluation"]["best_model"] == "LightGBM"
    assert rep["evaluation"]["metric_name"] == "accuracy"
    assert rep["evaluation"]["metric_direction"] == "higher_is_better"
    assert rep["evaluation"]["metric_value"] == 0.96
    assert rep["evaluation"]["oof_metric_value"] == 0.94
    assert len(rep["evaluation"]["leaderboard_top"]) == 3
    assert "artifacts" in rep["artifacts"]["model_dir"] or "m01" in rep["artifacts"]["model_dir"]
    assert "next_actions" in rep
    assert any("predict_tabular" in a for a in rep["next_actions"])


def test_persist_and_load_report_roundtrip(stub_predictor, isolated_artifacts):
    from tools import report
    task = _make_task()
    model_path("m01").mkdir(parents=True, exist_ok=True)
    rep = report.assemble_report(
        predictor_type="tabular", task=task, params=task.params,
        data_summary={"target": "segment", "rows_trained": 800},
        training_config_extra={}, predictor=stub_predictor,
        leaderboard=stub_predictor.leaderboard(),
        fit_summary=stub_predictor.fit_summary(), duration_seconds=5.0)
    rel = report.persist_report("m01", rep)
    assert "report.json" in rel
    loaded = report.load_report("m01")
    assert loaded is not None
    assert loaded["model_id"] == "m01"
    # download_urls must NOT be persisted.
    assert "download_urls" not in loaded


def test_detect_leakage_triggered(stub_predictor, isolated_artifacts):
    from tools import report
    fi = pd.DataFrame({
        "feature": ["is_seed", "age"],
        "importance": [0.95, 0.18],
    })
    msg = report.detect_leakage(fi)
    assert msg is not None
    assert "is_seed" in msg
    assert "0.9" in msg


def test_detect_leakage_absent(stub_predictor, isolated_artifacts):
    from tools import report
    fi = pd.DataFrame({
        "feature": ["age", "income"],
        "importance": [0.4, 0.3],
    })
    assert report.detect_leakage(fi) is None


def test_get_training_report_legacy_missing(stub_predictor, isolated_artifacts):
    from tools import report
    # Empty model dir with no report.json
    model_path("m_legacy").mkdir(parents=True, exist_ok=True)
    out = report.get_training_report("m_legacy")  # public, safe_tool-wrapped
    assert out["success"] is False
    assert "report.json not found" in out.get("error", "")


def test_metric_direction_lower_is_better(stub_predictor, isolated_artifacts):
    """For RMSE-like metrics, direction must be lower_is_better."""
    from tools import report
    em = MagicMock()
    em.name = "rmse"
    em.greater_is_better = False
    stub_predictor.eval_metric = em
    task = _make_task()
    model_path("m01").mkdir(parents=True, exist_ok=True)
    rep = report.assemble_report(
        predictor_type="tabular", task=task, params=task.params,
        data_summary={"target": "y"}, training_config_extra={},
        predictor=stub_predictor, leaderboard=stub_predictor.leaderboard(),
        fit_summary=stub_predictor.fit_summary(), duration_seconds=1.0)
    assert rep["evaluation"]["metric_direction"] == "lower_is_better"
