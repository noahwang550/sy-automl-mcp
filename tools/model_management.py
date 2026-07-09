"""Model management tools: list, load, delete trained models."""
from __future__ import annotations

from typing import Any

from config import (
    directory_size_bytes,
    list_registry_models,
    model_path,
    remove_model,
    validate_id,
)

from ._common import envelope_call

# Simple in-memory cache for loaded predictors. Keys are model_ids.
_model_cache: dict[str, Any] = {}


def _registry_entry(model_id: str) -> dict | None:
    for entry in list_registry_models():
        if entry.get("model_id") == model_id:
            return entry
    return None


def _model_dir_exists(model_id: str) -> bool:
    return model_path(model_id).exists()


def _load_predictor_obj(entry: dict) -> Any:
    model_type = entry.get("type")
    path = str(entry.get("path"))
    if model_type == "tabular":
        from autogluon.tabular import TabularPredictor

        return TabularPredictor.load(path)
    if model_type == "timeseries":
        from autogluon.timeseries import TimeSeriesPredictor

        return TimeSeriesPredictor.load(path)
    if model_type == "multimodal":
        from autogluon.multimodal import MultiModalPredictor

        return MultiModalPredictor.load(path)
    raise ValueError(f"Unsupported model type: {model_type}")


def _list_models(
    model_type: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
) -> dict[str, Any]:
    entries = list_registry_models()
    filtered: list[dict] = []
    for entry in entries:
        if model_type and entry.get("type") != model_type:
            continue
        created = entry.get("created_at")
        if created_after and created and str(created) <= created_after:
            continue
        if created_before and created and str(created) >= created_before:
            continue
        filtered.append(entry)
    return {"models": filtered, "count": len(filtered)}


def list_models(
    model_type: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
) -> dict[str, Any]:
    """List registered models with optional filters."""
    return envelope_call(_list_models, model_type, created_after, created_before)


def _model_info(model_id: str) -> dict[str, Any]:
    validate_id(model_id, "model_id")
    entry = _registry_entry(model_id)
    if entry is None:
        raise FileNotFoundError(f"Model not found in registry: {model_id}")
    return {"model_id": model_id, "info": entry}


def model_info(model_id: str) -> dict[str, Any]:
    """Return registry metadata for a model."""
    return envelope_call(_model_info, model_id)


def _load_model(model_id: str) -> dict[str, Any]:
    validate_id(model_id, "model_id")
    entry = _registry_entry(model_id)
    if entry is None:
        raise FileNotFoundError(f"Model not found in registry: {model_id}")
    if model_id not in _model_cache:
        _model_cache[model_id] = _load_predictor_obj(entry)
    return {
        "model_id": model_id,
        "status": "loaded",
        "info": entry,
    }


def load_model(model_id: str) -> dict[str, Any]:
    """Load a trained model into memory and cache it."""
    return envelope_call(_load_model, model_id)


def _delete_model(model_id: str, confirm: bool) -> dict[str, Any]:
    validate_id(model_id, "model_id")
    if not confirm:
        raise ValueError("delete_model requires confirm=True")
    entry = _registry_entry(model_id)
    if entry is None and not _model_dir_exists(model_id):
        raise FileNotFoundError(f"Model not found: {model_id}")
    path = model_path(model_id)
    freed = 0.0
    if path.exists():
        freed = directory_size_bytes(path) / (1024 * 1024)
        import shutil

        shutil.rmtree(path)
    remove_model(model_id)
    _model_cache.pop(model_id, None)
    return {"deleted": True, "model_id": model_id, "freed_mb": round(freed, 2)}


def delete_model(model_id: str, confirm: bool = False) -> dict[str, Any]:
    """Delete a model directory and remove it from the registry."""
    return envelope_call(_delete_model, model_id, confirm)


# Backwards-compatible alias used by the plan.
load_predictor = load_model
