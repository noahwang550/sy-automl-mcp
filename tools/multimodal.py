"""MultimodalPredictor MCP tools (image / text / multimodal).

AutoGluon is imported lazily so that importing this module is cheap when
AutoGluon is not installed.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from config import (
    ARTIFACTS_DIR,
    INLINE_ROW_THRESHOLD,
    directory_size_bytes,
    model_path,
    prediction_path,
    register_model,
    validate_id,
)
from serialization import failure, to_jsonable
from tasks import get_task_manager
from tasks.manager import Task

from ._common import envelope_call
from .data import read_dataset_df, read_inline_or_dataset

_VALID_PROBLEM_TYPES = {
    "classification",
    "binary",
    "multiclass",
    "regression",
    "image_classification",
    "image_regression",
    "text_classification",
    "text_regression",
    "multimodal",
}


def _import_predictor():
    from autogluon.multimodal import MultiModalPredictor  # heavy; lazy

    return MultiModalPredictor


def _load_predictor(model_id: str):
    MultiModalPredictor = _import_predictor()
    path = model_path(model_id)
    if not path.exists():
        raise FileNotFoundError(f"Model not found: {model_id} (looked in {path})")
    return MultiModalPredictor.load(str(path))


# ---------------------------------------------------------------------------
# train
# ---------------------------------------------------------------------------


def _train_multimodal_job(task: Task) -> dict[str, Any]:
    p = task.params
    df = read_dataset_df(p["dataset_id"])
    label = p["label"]
    if label not in df.columns:
        raise ValueError(f"Label column not found: {label}")
    for col in (p.get("image_path_column"), p.get("text_column")):
        if col is not None and col not in df.columns:
            raise ValueError(f"Column not found: {col}")
    image_col = p.get("image_path_column")
    if image_col is not None:
        missing_imgs = []
        for img in df[image_col].dropna().astype(str):
            if not Path(img).exists() and not (ARTIFACTS_DIR / img).exists():
                missing_imgs.append(img)
        if missing_imgs:
            raise FileNotFoundError(
                f"Missing image files in column {image_col}: {missing_imgs[:3]}..."
            )
    problem_type = p.get("problem_type") or "multimodal"
    if problem_type in {"multimodal", "classification"}:
        problem_type = None
    MultiModalPredictor = _import_predictor()
    predictor = MultiModalPredictor(
        label=label,
        problem_type=problem_type,
        path=str(model_path(p["model_id"])),
        eval_metric=p.get("eval_metric"),
        verbosity=0,
    )
    predictor.fit(
        train_data=df,
        time_limit=p.get("time_limit"),
        presets=p.get("presets"),
    )
    task.artifact_path = str(model_path(p["model_id"]))
    size_mb = directory_size_bytes(model_path(p["model_id"])) / (1024 * 1024)
    register_model(
        {
            "model_id": p["model_id"],
            "type": "multimodal",
            "path": task.artifact_path,
            "target": label,
            "problem_type": problem_type,
            "created_at": task.created_at,
            "task_id": task.task_id,
            "size_mb": round(size_mb, 2),
        }
    )
    return {"model_id": p["model_id"], "artifact_path": task.artifact_path}


def train_multimodal(
    dataset_id: str,
    model_id: str,
    label: str,
    problem_type: str = "multimodal",
    image_path_column: str | None = None,
    text_column: str | None = None,
    eval_metric: str | None = None,
    time_limit: int | None = 600,
    presets: str | None = "medium_quality",
) -> dict[str, Any]:
    """Train a MultiModalPredictor in the background.

    Returns ``{task_id, model_id}`` immediately.
    """
    validate_id(dataset_id, "dataset_id")
    validate_id(model_id, "model_id")
    if problem_type not in _VALID_PROBLEM_TYPES:
        raise ValueError(f"Invalid problem_type: {problem_type}")
    params = {
        "dataset_id": dataset_id,
        "model_id": model_id,
        "label": label,
        "problem_type": problem_type,
        "image_path_column": image_path_column,
        "text_column": text_column,
        "eval_metric": eval_metric,
        "time_limit": time_limit,
        "presets": presets,
    }
    task_id = get_task_manager().submit("train_multimodal", _train_multimodal_job, params)
    return envelope_call(lambda: {"task_id": task_id, "model_id": model_id})


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------


def _predict_multimodal_job(task: Task) -> dict[str, Any]:
    p = task.params
    predictor = _load_predictor(p["model_id"])
    df = read_inline_or_dataset(p.get("dataset_id"), p.get("inline_csv"))
    preds = predictor.predict(df)
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


def predict_multimodal(
    model_id: str,
    dataset_id: str | None = None,
    inline_csv: str | None = None,
    prediction_id: str | None = None,
) -> dict[str, Any]:
    """Predict with a trained multimodal model.

    Provide exactly one of ``dataset_id`` or ``inline_csv``.
    """
    validate_id(model_id, "model_id")
    if (dataset_id is None) == (inline_csv is None):
        return failure("Provide exactly one of dataset_id or inline_csv")
    if prediction_id is None:
        prediction_id = f"pred_{uuid.uuid4().hex[:8]}"
    validate_id(prediction_id, "prediction_id")

    if dataset_id is not None:
        nrows = len(read_dataset_df(dataset_id))
    else:
        from io import StringIO

        nrows = sum(1 for _ in StringIO(inline_csv or "")) - 1

    params = {
        "model_id": model_id,
        "dataset_id": dataset_id,
        "inline_csv": inline_csv,
        "prediction_id": prediction_id,
    }
    if nrows <= INLINE_ROW_THRESHOLD:
        task = Task(task_id="inline", type="predict_multimodal", params=params)
        return envelope_call(_predict_multimodal_job, task)

    task_id = get_task_manager().submit("predict_multimodal", _predict_multimodal_job, params)
    return envelope_call(lambda: {"task_id": task_id, "prediction_id": prediction_id})


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def _evaluate_multimodal(model_id: str, dataset_id: str, metrics: list[str] | None = None) -> dict[str, Any]:
    predictor = _load_predictor(model_id)
    df = read_dataset_df(dataset_id)
    # MultiModalPredictor.evaluate returns a dict of metrics.
    score = predictor.evaluate(df, metrics=metrics)
    return {"model_id": model_id, "dataset_id": dataset_id, "metrics": to_jsonable(score)}


def evaluate_multimodal(
    model_id: str, dataset_id: str, metrics: list[str] | None = None
) -> dict[str, Any]:
    """Evaluate a trained multimodal model on a dataset."""
    validate_id(model_id, "model_id")
    validate_id(dataset_id, "dataset_id")
    return envelope_call(_evaluate_multimodal, model_id, dataset_id, metrics)
