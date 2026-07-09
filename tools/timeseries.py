"""TimeSeriesPredictor MCP tools.

AutoGluon is imported lazily so that importing this module is cheap when
AutoGluon is not installed.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pandas as pd

from config import (
    INLINE_ROW_THRESHOLD,
    directory_size_bytes,
    get_registry_entry,
    model_path,
    prediction_path,
    register_model,
    validate_id,
)
from serialization import to_jsonable
from tasks import get_task_manager
from tasks.manager import Task

from ._common import envelope_call, safe_tool
from .data import read_dataset_df


def _import_predictor():
    from autogluon.timeseries import TimeSeriesPredictor  # heavy; lazy

    return TimeSeriesPredictor


def _import_dataframe():
    from autogluon.timeseries import TimeSeriesDataFrame  # heavy; lazy

    return TimeSeriesDataFrame


def _to_tsdf(df, id_column: str | None, time_column: str | None):
    TimeSeriesDataFrame = _import_dataframe()
    return TimeSeriesDataFrame.from_data_frame(
        df,
        id_column=id_column,
        timestamp_column=time_column,
    )


def _load_predictor(model_id: str):
    TimeSeriesPredictor = _import_predictor()
    path = model_path(model_id)
    if not path.exists():
        raise FileNotFoundError(f"Model not found: {model_id} (looked in {path})")
    return TimeSeriesPredictor.load(str(path))


# ---------------------------------------------------------------------------
# train
# ---------------------------------------------------------------------------


def _train_timeseries_job(task: Task) -> dict[str, Any]:
    p = task.params
    df = read_dataset_df(p["dataset_id"])
    ts_df = _to_tsdf(df, p.get("id_column"), p.get("time_column"))
    TimeSeriesPredictor = _import_predictor()
    predictor = TimeSeriesPredictor(
        target=p["target"],
        path=str(model_path(p["model_id"])),
        prediction_length=p["prediction_length"],
        eval_metric=p.get("eval_metric"),
        freq=p.get("freq"),
        verbosity=0,
    )
    predictor.fit(
        train_data=ts_df,
        time_limit=p.get("time_limit"),
        presets=p.get("presets"),
        verbosity=0,
    )
    task.artifact_path = str(model_path(p["model_id"]))
    size_mb = directory_size_bytes(model_path(p["model_id"])) / (1024 * 1024)
    register_model(
        {
            "model_id": p["model_id"],
            "type": "timeseries",
            "path": task.artifact_path,
            "target": p["target"],
            "problem_type": "timeseries",
            "created_at": task.created_at,
            "task_id": task.task_id,
            "size_mb": round(size_mb, 2),
            "dataset_id": p["dataset_id"],
            "id_column": p.get("id_column"),
            "time_column": p.get("time_column"),
        }
    )
    lb = predictor.leaderboard(silent=True)
    return {
        "model_id": p["model_id"],
        "artifact_path": task.artifact_path,
        "leaderboard_top": to_jsonable(lb.head(5)) if lb is not None else [],
    }


def train_timeseries(
    dataset_id: str,
    target: str,
    model_id: str,
    prediction_length: int,
    time_column: str,
    id_column: str | None = None,
    freq: str | None = None,
    eval_metric: str | None = None,
    time_limit: int | None = 600,
    presets: str | None = "medium_quality",
) -> dict[str, Any]:
    """Train a TimeSeriesPredictor in the background.

    Returns ``{task_id, model_id}`` immediately.
    """
    validate_id(dataset_id, "dataset_id")
    validate_id(model_id, "model_id")
    if prediction_length <= 0:
        raise ValueError("prediction_length must be positive")
    params = {
        "dataset_id": dataset_id,
        "target": target,
        "model_id": model_id,
        "prediction_length": prediction_length,
        "time_column": time_column,
        "id_column": id_column,
        "freq": freq,
        "eval_metric": eval_metric,
        "time_limit": time_limit,
        "presets": presets,
    }
    task_id = get_task_manager().submit("train_timeseries", _train_timeseries_job, params)
    return envelope_call(lambda: {"task_id": task_id, "model_id": model_id})


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------


def _predict_timeseries_job(task: Task) -> dict[str, Any]:
    p = task.params
    predictor = _load_predictor(p["model_id"])
    if p.get("dataset_id"):
        df = read_dataset_df(p["dataset_id"])
        data = _to_tsdf(df, p.get("id_column"), p.get("time_column"))
    else:
        # TimeSeriesPredictor.predict requires data. Fall back to the training
        # dataset recorded in the registry when the caller omits a dataset_id.
        entry = get_registry_entry(p["model_id"])
        if entry is None or not entry.get("dataset_id"):
            raise ValueError(
                "No dataset provided and training dataset not found in registry"
            )
        df = read_dataset_df(entry["dataset_id"])
        data = _to_tsdf(df, entry.get("id_column"), entry.get("time_column"))
    preds = predictor.predict(data)
    if isinstance(preds, pd.DataFrame):
        preds = preds.reset_index()
    out = prediction_path(p["prediction_id"])
    out.write_text(
        json.dumps(
            {"model_id": p["model_id"], "predictions": to_jsonable(preds)}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    return {
        "model_id": p["model_id"],
        "prediction_id": p["prediction_id"],
        "predictions": to_jsonable(preds),
        "path": str(out),
    }


def predict_timeseries(
    model_id: str,
    dataset_id: str | None = None,
    id_column: str | None = None,
    time_column: str | None = None,
    prediction_id: str | None = None,
) -> dict[str, Any]:
    """Predict with a trained time-series model.

    Provide ``dataset_id`` to forecast on new data; omit it to forecast the
    training data's future.
    """
    validate_id(model_id, "model_id")
    if dataset_id is not None:
        validate_id(dataset_id, "dataset_id")
    if prediction_id is None:
        prediction_id = f"pred_{uuid.uuid4().hex[:8]}"
    validate_id(prediction_id, "prediction_id")

    params = {
        "model_id": model_id,
        "dataset_id": dataset_id,
        "id_column": id_column,
        "time_column": time_column,
        "prediction_id": prediction_id,
    }
    # Forecasting on a small dataset is fast enough to inline; large datasets
    # go to the background task manager.
    if dataset_id is None:
        # Future forecast without data is usually tiny.
        task = Task(task_id="inline", type="predict_timeseries", params=params)
        return envelope_call(_predict_timeseries_job, task)

    nrows = len(read_dataset_df(dataset_id))
    if nrows <= INLINE_ROW_THRESHOLD:
        task = Task(task_id="inline", type="predict_timeseries", params=params)
        return envelope_call(_predict_timeseries_job, task)

    task_id = get_task_manager().submit("predict_timeseries", _predict_timeseries_job, params)
    return envelope_call(lambda: {"task_id": task_id, "prediction_id": prediction_id})


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


def _leaderboard_timeseries(model_id: str) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    lb = predictor.leaderboard(silent=True)
    return {"model_id": model_id, "leaderboard": to_jsonable(lb)}


def leaderboard_timeseries(model_id: str) -> dict[str, Any]:
    """Return the time-series model leaderboard."""
    validate_id(model_id, "model_id")
    return envelope_call(_leaderboard_timeseries, model_id)


def _evaluate_timeseries(
    model_id: str,
    dataset_id: str,
    id_column: str | None = None,
    time_column: str | None = None,
    metrics: list[str] | None = None,
) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    df = read_dataset_df(dataset_id)
    data = _to_tsdf(df, id_column, time_column)
    score = predictor.evaluate(data, metrics=metrics)
    return {"model_id": model_id, "dataset_id": dataset_id, "metrics": to_jsonable(score)}


def evaluate_timeseries(
    model_id: str,
    dataset_id: str,
    id_column: str | None = None,
    time_column: str | None = None,
    metrics: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate a trained time-series model on a dataset."""
    validate_id(model_id, "model_id")
    validate_id(dataset_id, "dataset_id")
    return envelope_call(_evaluate_timeseries, model_id, dataset_id, id_column, time_column, metrics)


def _fit_summary_timeseries(model_id: str) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    summary = predictor.fit_summary(verbosity=0)
    return {"model_id": model_id, "summary": to_jsonable(summary)}


def fit_summary_timeseries(model_id: str) -> dict[str, Any]:
    """Return predictor.fit_summary() for a time-series model."""
    validate_id(model_id, "model_id")
    return envelope_call(_fit_summary_timeseries, model_id)

# Wrap public tools so direct imports also return the unified envelope.
train_timeseries = safe_tool(train_timeseries)
predict_timeseries = safe_tool(predict_timeseries)
leaderboard_timeseries = safe_tool(leaderboard_timeseries)
evaluate_timeseries = safe_tool(evaluate_timeseries)
fit_summary_timeseries = safe_tool(fit_summary_timeseries)
