"""Centralized configuration: paths, env vars, registry helpers.

All artifact paths resolve under a single root (``ARTIFACTS_DIR``) which is
bind-mounted into the container at ``/app/artifacts``. Tool code must never
accept raw absolute paths from callers — it resolves user-supplied identifiers
against this root and rejects traversal attempts.

Environment variables consumed here:
    - MCP_TRANSPORT, MCP_HOST, MCP_PORT
    - MCP_MAX_WORKERS, MCP_MODEL_CACHE_MAX
    - MCP_TASK_RETENTION_SECONDS, MCP_TASK_MAX_RETAINED
    - MCP_API_TOKEN (optional; when set, streamable-http requires Bearer token)
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", Path(__file__).resolve().parent / "artifacts"))
DATASETS_DIR = ARTIFACTS_DIR / "datasets"
MODELS_DIR = ARTIFACTS_DIR / "models"
PREDICTIONS_DIR = ARTIFACTS_DIR / "predictions"
LOGS_DIR = ARTIFACTS_DIR / "logs"
REGISTRY_PATH = ARTIFACTS_DIR / "registry.json"

# ---------------------------------------------------------------------------
# Runtime env
# ---------------------------------------------------------------------------

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_MAX_WORKERS = max(1, int(os.environ.get("MCP_MAX_WORKERS", "1")))
MCP_MODEL_CACHE_MAX = max(1, int(os.environ.get("MCP_MODEL_CACHE_MAX", "4")))
MCP_TASK_RETENTION_SECONDS = max(0, int(os.environ.get("MCP_TASK_RETENTION_SECONDS", "86400")))
MCP_TASK_MAX_RETAINED = max(0, int(os.environ.get("MCP_TASK_MAX_RETAINED", "100")))
MCP_API_TOKEN_RAW = os.environ.get("MCP_API_TOKEN")
MCP_API_TOKEN: str | None = MCP_API_TOKEN_RAW if MCP_API_TOKEN_RAW else None

# Rows above which predict/evaluate run as background tasks instead of inline.
INLINE_ROW_THRESHOLD = int(os.environ.get("INLINE_ROW_THRESHOLD", "5000"))

# Resource limits for datasets (can be lowered in test environments).
MAX_DATASET_ROWS = int(os.environ.get("MAX_DATASET_ROWS", "1000000"))
MAX_DATASET_MB = int(os.environ.get("MAX_DATASET_MB", "1024"))
MAX_DATASET_COLUMNS = int(os.environ.get("MAX_DATASET_COLUMNS", "10000"))

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def ensure_dirs() -> None:
    """Create the artifact directory tree. Idempotent."""
    for d in (ARTIFACTS_DIR, DATASETS_DIR, MODELS_DIR, PREDICTIONS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def configure(artifacts_dir: Path) -> None:
    """Reassign all path globals to a new root (used by tests for isolation).

    Module-level functions read these globals at call time, so reassigning here
    takes effect for subsequent calls.
    """
    global ARTIFACTS_DIR, DATASETS_DIR, MODELS_DIR, PREDICTIONS_DIR, LOGS_DIR, REGISTRY_PATH
    ARTIFACTS_DIR = Path(artifacts_dir)
    DATASETS_DIR = ARTIFACTS_DIR / "datasets"
    MODELS_DIR = ARTIFACTS_DIR / "models"
    PREDICTIONS_DIR = ARTIFACTS_DIR / "predictions"
    LOGS_DIR = ARTIFACTS_DIR / "logs"
    REGISTRY_PATH = ARTIFACTS_DIR / "registry.json"
    ensure_dirs()


def validate_id(name: str, label: str = "id") -> str:
    """Reject identifiers that could escape the artifacts root.

    Only safe filename characters are allowed; no path separators, no ``..``.
    """
    if not name or not _SAFE_ID_RE.match(name):
        raise ValueError(f"Invalid {label}: {name!r} (allowed: letters, digits, _ . -)")
    if name in {".", ".."}:
        raise ValueError(f"Reserved {label}: {name!r}")
    return name


def model_path(model_id: str) -> Path:
    validate_id(model_id, "model_id")
    return MODELS_DIR / model_id


def dataset_path(dataset_id: str) -> Path:
    validate_id(dataset_id, "dataset_id")
    return DATASETS_DIR / dataset_id


def prediction_path(prediction_id: str) -> Path:
    validate_id(prediction_id, "prediction_id")
    return PREDICTIONS_DIR / f"{prediction_id}.json"


def log_path(task_id: str) -> Path:
    validate_id(task_id, "task_id")
    return LOGS_DIR / f"{task_id}.log"


def directory_size_bytes(path: Path) -> int:
    """Return the total byte size of a directory tree."""
    total = 0
    if path.is_file():
        return path.stat().st_size
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


# ---------------------------------------------------------------------------
# Model registry (registry.json) — locked, JSON-serializable
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()


def _load_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        with REGISTRY_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_registry(entries: list[dict]) -> None:
    ensure_dirs()
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    tmp.replace(REGISTRY_PATH)


def register_model(entry: dict) -> None:
    """Add or replace a model entry by ``model_id`` (thread-safe)."""
    with _registry_lock:
        entries = _load_registry()
        mid = entry["model_id"]
        entries = [e for e in entries if e.get("model_id") != mid]
        entries.append(entry)
        _write_registry(entries)


def remove_model(model_id: str) -> None:
    with _registry_lock:
        entries = [e for e in _load_registry() if e.get("model_id") != model_id]
        _write_registry(entries)


def list_registry_models() -> list[dict]:
    with _registry_lock:
        return _load_registry()


def get_registry_entry(model_id: str) -> dict | None:
    """Return a single registry entry by model_id, or None if absent."""
    with _registry_lock:
        for entry in _load_registry():
            if entry.get("model_id") == model_id:
                return entry
    return None
