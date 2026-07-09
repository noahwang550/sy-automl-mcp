"""Tests for Multimodal tools.

AutoGluon-dependent tests are skipped when ``autogluon.multimodal`` is not
installed. Pure-Python validation tests still run in the lightweight image.
"""
from __future__ import annotations

import pytest

from tools.data import load_dataset

pytest.importorskip("autogluon.multimodal")

import time  # noqa: E402

from tasks.manager import FAILED, SUCCESS  # noqa: E402
from tools.multimodal import (  # noqa: E402
    evaluate_multimodal,
    predict_multimodal,
    train_multimodal,
)


def _mm_csv() -> str:
    # MultiModalPredictor needs enough text samples to fit a model.
    rows = ["text,label"]
    for i in range(20):
        label = i % 2
        rows.append(f"sample text number {i} with some words,{label}")
    return "\n".join(rows) + "\n"


def _data(result):
    assert result["success"] is True, result.get("error")
    return result["data"]


def _wait(task_id, mgr, timeout=120):
    for _ in range(int(timeout / 0.5)):
        st = mgr.status(task_id)["status"]
        if st in {SUCCESS, FAILED}:
            return st
        time.sleep(0.5)
    return mgr.status(task_id)["status"]


def test_train_multimodal_and_predict(isolated_artifacts):
    from tasks import get_task_manager

    mgr = get_task_manager()
    load_dataset(source=_mm_csv(), dataset_id="mm")
    res = train_multimodal(
        dataset_id="mm",
        model_id="mm_model",
        label="label",
        problem_type="binary",
        text_column="text",
        time_limit=60,
        presets="medium_quality",
    )
    payload = _data(res)
    assert payload["model_id"] == "mm_model"
    assert "task_id" in payload
    status = _wait(payload["task_id"], mgr)
    assert status == SUCCESS, mgr.result(payload["task_id"])

    pred = _data(predict_multimodal(model_id="mm_model", dataset_id="mm"))
    assert "predictions" in pred

    eval_res = _data(evaluate_multimodal("mm_model", "mm"))
    assert "metrics" in eval_res
