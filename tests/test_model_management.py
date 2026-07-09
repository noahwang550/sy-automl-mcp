"""Tests for model management tools (no AutoGluon required)."""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from tools.model_management import delete_model, list_models, model_info


def _data(result):
    assert result["success"] is True, result.get("error")
    return result["data"]


def test_list_models_empty(isolated_artifacts):
    res = list_models()
    data = _data(res)
    assert data["models"] == []
    assert data["count"] == 0


def test_model_info_and_list(isolated_artifacts):
    config.register_model(
        {
            "model_id": "m1",
            "type": "tabular",
            "path": "/tmp/m1",
            "target": "y",
            "problem_type": "auto",
            "created_at": 1.0,
            "task_id": "t1",
            "size_mb": 0.1,
        }
    )
    res = list_models()
    data = _data(res)
    assert data["count"] == 1
    assert data["models"][0]["model_id"] == "m1"

    info = _data(model_info("m1"))
    assert info["model_id"] == "m1"
    assert info["info"]["type"] == "tabular"


def test_list_models_filter_by_type(isolated_artifacts):
    config.register_model(
        {
            "model_id": "m2",
            "type": "timeseries",
            "path": "/tmp/m2",
            "target": "y",
            "problem_type": "timeseries",
            "created_at": 2.0,
            "task_id": "t2",
            "size_mb": 0.2,
        }
    )
    res = list_models(model_type="timeseries")
    data = _data(res)
    assert data["count"] == 1
    assert data["models"][0]["model_id"] == "m2"


def test_delete_model_requires_confirm(isolated_artifacts):
    res = delete_model("m1", confirm=False)
    assert res["success"] is False
    assert "confirm=True" in res["error"]


def test_delete_model_removes_registry_entry(isolated_artifacts):
    config.register_model(
        {
            "model_id": "m3",
            "type": "tabular",
            "path": "/tmp/m3",
            "target": "y",
            "problem_type": "auto",
            "created_at": 3.0,
            "task_id": "t3",
            "size_mb": 0.3,
        }
    )
    # No real model directory, so freed_mb will be 0 but registry entry is removed.
    res = delete_model("m3", confirm=True)
    data = _data(res)
    assert data["deleted"] is True
    assert data["model_id"] == "m3"

    info = model_info("m3")
    assert info["success"] is False


def test_model_info_not_found(isolated_artifacts):
    res = model_info("no-such-model")
    assert res["success"] is False
    assert "not found" in res["error"].lower()
