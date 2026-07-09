"""Tests for TimeSeries tools.

AutoGluon-dependent tests are skipped when ``autogluon.timeseries`` is not
installed. Pure-Python validation tests still run in the lightweight image.
"""
from __future__ import annotations

import pytest

from tools.data import load_dataset

pytest.importorskip("autogluon.timeseries")

import time  # noqa: E402

from tasks.manager import FAILED, SUCCESS  # noqa: E402
from tools.timeseries import (  # noqa: E402
    evaluate_timeseries,
    fit_summary_timeseries,
    leaderboard_timeseries,
    predict_timeseries,
    train_timeseries,
)


def _tsv_csv() -> str:
    # TimeSeriesPredictor needs >= 7 observations per series.
    rows = ["item_id,timestamp,target"]
    for item in "AB":
        for day, val in enumerate(range(1, 10), start=1):
            rows.append(f"{item},2023-01-{day:02d},{val + ord(item)}")
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


def test_train_timeseries_and_predict(isolated_artifacts):
    from tasks import get_task_manager

    mgr = get_task_manager()
    load_dataset(source=_tsv_csv(), dataset_id="ts")
    res = train_timeseries(
        dataset_id="ts",
        target="target",
        model_id="ts_model",
        prediction_length=2,
        time_column="timestamp",
        id_column="item_id",
        time_limit=30,
        presets=None,
    )
    payload = _data(res)
    assert payload["model_id"] == "ts_model"
    assert "task_id" in payload
    status = _wait(payload["task_id"], mgr)
    assert status == SUCCESS, mgr.result(payload["task_id"])

    lb = _data(leaderboard_timeseries("ts_model"))
    assert "leaderboard" in lb

    pred = _data(predict_timeseries(model_id="ts_model"))
    assert "predictions" in pred

    summary = _data(fit_summary_timeseries("ts_model"))
    assert "summary" in summary

    eval_res = _data(evaluate_timeseries("ts_model", "ts"))
    assert "metrics" in eval_res
