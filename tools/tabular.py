"""TabularPredictor MCP tools (Phase 1 MVP).

AutoGluon is imported lazily inside each function so the module imports cheaply
(unit tests that don't touch AutoGluon don't pay the multi-second import cost).
"""
from __future__ import annotations

import json
import uuid
from io import StringIO
from typing import Any

import pandas as pd

from config import (
    INLINE_ROW_THRESHOLD,
    model_path,
    prediction_path,
    register_model,
    validate_id,
)
from serialization import failure, to_jsonable
from tasks import get_task_manager
from tasks.manager import Task

from ._common import envelope_call, safe_tool
from .data import read_dataset_df, read_inline_or_dataset


def _import_predictor():
    from autogluon.tabular import TabularPredictor  # heavy; lazy

    return TabularPredictor


def _load_predictor(model_id: str):
    TabularPredictor = _import_predictor()
    path = model_path(model_id)
    if not path.exists():
        raise FileNotFoundError(f"Model not found: {model_id} (looked in {path})")
    return TabularPredictor.load(str(path))


def _resolve_problem_type(problem_type: str | None) -> str | None:
    if problem_type is None or problem_type == "auto":
        return None
    return problem_type


# ---------------------------------------------------------------------------
# train
# ---------------------------------------------------------------------------


def _train_tabular_job(task: Task) -> dict[str, Any]:
    p = task.params
    df = read_dataset_df(p["dataset_id"])
    TabularPredictor = _import_predictor()
    predictor = TabularPredictor(
        label=p["target"],
        path=str(model_path(p["model_id"])),
        problem_type=_resolve_problem_type(p.get("problem_type")),
        eval_metric=p.get("eval_metric"),
        verbosity=0,
    )
    predictor.fit(
        train_data=df,
        time_limit=p.get("time_limit"),
        presets=p.get("presets"),
        hyperparameters=p.get("hyperparameters"),
        verbosity=0,
    )
    task.artifact_path = str(model_path(p["model_id"]))
    # Persist to registry so list_models can find it.
    from config import directory_size_bytes

    size_mb = directory_size_bytes(model_path(p["model_id"])) / (1024 * 1024)
    register_model(
        {
            "model_id": p["model_id"],
            "type": "tabular",
            "path": task.artifact_path,
            "target": p["target"],
            "problem_type": p.get("problem_type") or "auto",
            "created_at": task.created_at,
            "task_id": task.task_id,
            "size_mb": round(size_mb, 2),
        }
    )
    lb = predictor.leaderboard(silent=True)
    best = str(predictor.model_best) if predictor.model_best else None
    return {
        "model_id": p["model_id"],
        "best_model": best,
        "artifact_path": task.artifact_path,
        "leaderboard_top": to_jsonable(lb.head(5)) if lb is not None else [],
    }


def train_tabular(
    dataset_id: str,
    target: str,
    model_id: str,
    problem_type: str | None = "auto",
    eval_metric: str | None = None,
    time_limit: int | None = 600,
    presets: str | None = "medium_quality",
    hyperparameters: dict | None = None,
    random_seed: int | None = None,
) -> dict[str, Any]:
    """Train a TabularPredictor in the background. Returns ``{task_id, model_id}`` immediately.

    Always set a bounded ``time_limit`` (default 600s) — cancellation is soft
    and relies on AutoGluon honoring the time budget.
    """
    validate_id(dataset_id, "dataset_id")
    validate_id(model_id, "model_id")
    params = {
        "dataset_id": dataset_id,
        "target": target,
        "model_id": model_id,
        "problem_type": problem_type,
        "eval_metric": eval_metric,
        "time_limit": time_limit,
        "presets": presets,
        "hyperparameters": hyperparameters,
        "random_seed": random_seed,
    }
    task_id = get_task_manager().submit("train_tabular", _train_tabular_job, params)
    return envelope_call(lambda: {"task_id": task_id, "model_id": model_id})


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------


def _predict_tabular_job(task: Task) -> dict[str, Any]:
    p = task.params
    predictor = _load_predictor(p["model_id"])
    df = read_inline_or_dataset(p.get("dataset_id"), p.get("inline_csv"))
    preds = predictor.predict(df)
    out = prediction_path(p["prediction_id"])
    result = {"model_id": p["model_id"], "prediction_id": p["prediction_id"]}
    # Persist predictions as JSON records.
    payload = pd.DataFrame({"prediction": preds})
    payload["__row_index__"] = range(len(payload))
    out.write_text(
        json.dumps({"model_id": p["model_id"], "predictions": to_jsonable(payload)}, ensure_ascii=False),
        encoding="utf-8",
    )
    task.artifact_path = str(out)
    result["predictions"] = to_jsonable(preds)
    result["path"] = str(out)
    return result


def predict_tabular(
    model_id: str,
    dataset_id: str | None = None,
    inline_csv: str | None = None,
    prediction_id: str | None = None,
) -> dict[str, Any]:
    """Predict with a trained tabular model.

    Provide exactly one of ``dataset_id`` or ``inline_csv``. Small inputs run
    inline; large inputs (> INLINE_ROW_THRESHOLD rows) run as a background task
    and return ``{task_id, prediction_id}``.
    """
    validate_id(model_id, "model_id")
    if (dataset_id is None) == (inline_csv is None):
        return failure("Provide exactly one of dataset_id or inline_csv")
    if prediction_id is None:
        prediction_id = f"pred_{uuid.uuid4().hex[:8]}"
    validate_id(prediction_id, "prediction_id")

    # Estimate row count to decide inline vs background.
    if dataset_id is not None:
        df = read_dataset_df(dataset_id)
        nrows = len(df)
    else:
        nrows = sum(1 for _ in StringIO(inline_csv or "")) - 1

    params = {
        "model_id": model_id,
        "dataset_id": dataset_id,
        "inline_csv": inline_csv,
        "prediction_id": prediction_id,
    }
    if nrows <= INLINE_ROW_THRESHOLD:
        # Inline: run synchronously (still bounded; predictions are usually fast).
        task = Task(task_id="inline", type="predict_tabular", params=params)
        return envelope_call(_predict_tabular_job, task)

    task_id = get_task_manager().submit("predict_tabular", _predict_tabular_job, params)
    return envelope_call(lambda: {"task_id": task_id, "prediction_id": prediction_id})


# ---------------------------------------------------------------------------
# diagnostics (inline, fast)
# ---------------------------------------------------------------------------


def _leaderboard_tabular(model_id: str, extra_info: bool = False) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    lb = predictor.leaderboard(silent=True, extra_info=extra_info)
    return {
        "model_id": model_id,
        "leaderboard": to_jsonable(lb),
        "best_model": str(predictor.model_best) if predictor.model_best else None,
    }


def leaderboard_tabular(model_id: str, extra_info: bool = False) -> dict[str, Any]:
    """Return the model leaderboard as JSON-safe rows."""
    validate_id(model_id, "model_id")
    return envelope_call(_leaderboard_tabular, model_id, extra_info)


def _feature_importance_tabular(model_id: str, dataset_id: str | None = None) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    data = read_dataset_df(dataset_id) if dataset_id else None
    if data is not None:
        fi = predictor.feature_importance(data, silent=True)
    else:
        fi = predictor.feature_importance(None, silent=True, feature_stage="transformed")
    return {"model_id": model_id, "importances": to_jsonable(fi)}


def feature_importance_tabular(
    model_id: str, dataset_id: str | None = None
) -> dict[str, Any]:
    """Return feature importance for the best model."""
    validate_id(model_id, "model_id")
    return envelope_call(_feature_importance_tabular, model_id, dataset_id)


def _fit_summary_tabular(model_id: str) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    summary = predictor.fit_summary(verbosity=0)
    return {"model_id": model_id, "summary": to_jsonable(summary)}


def fit_summary_tabular(model_id: str) -> dict[str, Any]:
    """Return predictor.fit_summary() as JSON-safe dicts."""
    validate_id(model_id, "model_id")
    return envelope_call(_fit_summary_tabular, model_id)


def _evaluate_tabular(model_id: str, dataset_id: str, metrics: list[str] | None = None) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    df = read_dataset_df(dataset_id)
    # AutoGluon 1.5.0's TabularPredictor.evaluate() takes no `metric` kwarg;
    # it always returns a dict of all available metrics (the eval_metric plus
    # auxiliary_metrics). Call it once, then filter to the requested subset.
    scores: dict[str, Any] = predictor.evaluate(df)
    if metrics:
        wanted = {m: scores[m] for m in metrics if m in scores}
        scores = wanted
    return {"model_id": model_id, "dataset_id": dataset_id, "metrics": to_jsonable(scores)}


def evaluate_tabular(
    model_id: str, dataset_id: str, metrics: list[str] | None = None
) -> dict[str, Any]:
    """Evaluate the model on a dataset. Returns metric -> value."""
    validate_id(model_id, "model_id")
    validate_id(dataset_id, "dataset_id")
    return envelope_call(_evaluate_tabular, model_id, dataset_id, metrics)

# Wrap public tools so direct imports also return the unified envelope.
train_tabular = safe_tool(train_tabular)
predict_tabular = safe_tool(predict_tabular)
leaderboard_tabular = safe_tool(leaderboard_tabular)
feature_importance_tabular = safe_tool(feature_importance_tabular)
fit_summary_tabular = safe_tool(fit_summary_tabular)
evaluate_tabular = safe_tool(evaluate_tabular)
