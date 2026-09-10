"""Training report assembly + persistence.

After each ``_train_*_job`` finishes, the worker writes a ``report.json``
into the model directory capturing the full training process: data summary,
training config, evaluation results (leaderboard + fit_summary), and an
optional lazy feature_importance section (computed on first request via
``get_training_report(include_feature_importance=true)``).

Three predictor types (tabular / timeseries / multimodal) share the same
report skeleton; type-specific fields live under ``data_summary`` and are
``None`` when not applicable.

A per-model lock serializes feature_importance computation so two concurrent
``get_training_report`` calls don't both re-load the predictor.
"""
from __future__ import annotations

import importlib.metadata
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from config import (
    MCP_ARTIFACT_BASE_URL,
    MCP_DOWNLOAD_URL_TTL_SECONDS,
    dataset_path,
    get_registry_entry,
    log_path,
    model_path,
    validate_id,
)
import config as _config
from serialization import failure, success, to_jsonable
from tasks.manager import Task

from ._common import envelope_call, safe_tool
from .artifacts import build_presigned_token

SCHEMA_VERSION = 1
LEAKAGE_THRESHOLD = 0.9
LOG_TAIL_LINES = 50
TOP_FEATURES_N = 20
_LOG_TAIL_BYTES = 8192

_fi_locks: dict[str, threading.Lock] = {}
_fi_locks_guard = threading.Lock()


def _fi_lock(model_id: str) -> threading.Lock:
    with _fi_locks_guard:
        lock = _fi_locks.get(model_id)
        if lock is None:
            lock = threading.Lock()
            _fi_locks[model_id] = lock
        return lock


def _json_atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    tmp.replace(path)


def _iso(ts: float | None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _autogluon_version() -> str:
    for pkg in ("autogluon", "autogluon.core", "autogluon.tabular"):
        try:
            return importlib.metadata.version(pkg)
        except Exception:
            continue
    return "unknown"


def _read_log_tail(task_id: str | None, n: int = LOG_TAIL_LINES) -> list[str]:
    """Return up to ``n`` trailing lines of the task log. Never raises."""
    if not task_id:
        return []
    try:
        p = log_path(task_id)
        if not p.exists():
            return []
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-n:] if len(lines) >= n else lines
    except Exception:
        return []


def _detect_log_flags(task_id: str | None, duration: float,
                      time_limit: float | None) -> tuple[bool, bool]:
    """Heuristic: (time_limit_hit, early_stopped) from log + duration."""
    tail = "\n".join(_read_log_tail(task_id, 30))
    tl_hit = False
    if time_limit and duration >= 0.95 * time_limit:
        tl_hit = True
    if "Time limit" in tail or "time_limit" in tail and "reached" in tail.lower():
        tl_hit = True
    early = "Early stopping" in tail or "early_stop" in tail.lower()
    return tl_hit, early


# ---------------------------------------------------------------------------
# Feature importance (lazy) + leakage detection
# ---------------------------------------------------------------------------


def detect_leakage(fi_df: pd.DataFrame) -> str | None:
    """Return a warning string if any feature's importance > LEAKAGE_THRESHOLD."""
    if fi_df is None or fi_df.empty:
        return None
    col = "importance" if "importance" in fi_df.columns else None
    if col is None:
        return None
    try:
        max_row = fi_df.loc[fi_df[col].astype(float).idxmax()]
        max_val = float(max_row[col])
    except Exception:
        return None
    if max_val > LEAKAGE_THRESHOLD:
        name = str(max_row.get("feature", max_row.name))
        return (f"Feature {name!r} has importance {max_val:.3f} "
                f"(> {LEAKAGE_THRESHOLD}) — suspect label leakage")
    return None


def _feature_importance_tabular(predictor: Any, dataset_id: str | None) -> tuple[pd.DataFrame | None, str | None]:
    if dataset_id is None:
        return None, "dataset_unavailable"
    try:
        from .data import read_dataset_df
        df = read_dataset_df(dataset_id)
    except Exception:
        return None, "dataset_unavailable"
    try:
        fi = predictor.feature_importance(df, silent=True)
        return fi, None
    except Exception as e:
        return None, f"compute_failed: {e}"


def _feature_importance_timeseries(predictor: Any, dataset_id: str | None) -> tuple[pd.DataFrame | None, str | None]:
    if not hasattr(predictor, "feature_importance"):
        return None, "not_supported_for_timeseries"
    if dataset_id is None:
        return None, "dataset_unavailable"
    try:
        from .data import read_dataset_df
        df = read_dataset_df(dataset_id)
        fi = predictor.feature_importance(df)
        return fi, None
    except Exception as e:
        return None, f"compute_failed: {e}"


def compute_feature_importance(model_id: str, predictor_type: str,
                                dataset_id: str | None,
                                target: str | None) -> dict[str, Any]:
    """Compute and cache feature_importance for a model. Returns the FI section dict.

    Serializes per-model so two concurrent requests don't both load the predictor.
    Tabular goes through the model LRU cache; timeseries/multimodal load directly
    (those tiers are typically not installed in the running image anyway).
    """
    with _fi_lock(model_id):
        fi_csv = model_path(model_id) / "feature_importance.csv"
        if fi_csv.exists():
            try:
                cached = pd.read_csv(fi_csv)
                leakage = detect_leakage(cached)
                return {
                    "method": "permutation",
                    "computed_at": datetime.fromtimestamp(
                        fi_csv.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "top_features": to_jsonable(cached.head(TOP_FEATURES_N)),
                    "leakage_warning": leakage,
                    "full_path": str(fi_csv.relative_to(_config.ARTIFACTS_DIR.resolve())),
                    "reason": None,
                }
            except Exception:
                pass  # fall through to recompute

        if predictor_type == "multimodal":
            return {"method": None, "computed_at": None, "top_features": [],
                    "leakage_warning": None, "full_path": None,
                    "reason": "not_supported_for_multimodal"}

        # Load predictor and compute.
        try:
            if predictor_type == "tabular":
                from .model_management import _model_cache, _load_predictor_obj
                entry = get_registry_entry(model_id) or {"type": "tabular", "path": str(model_path(model_id))}
                predictor = _model_cache.get_or_load(
                    model_id, lambda: _load_predictor_obj(entry))
                fi, reason = _feature_importance_tabular(predictor, dataset_id)
            elif predictor_type == "timeseries":
                from .timeseries import _load_predictor
                predictor = _load_predictor(model_id)
                fi, reason = _feature_importance_timeseries(predictor, dataset_id)
            else:
                return {"method": None, "computed_at": None, "top_features": [],
                        "leakage_warning": None, "full_path": None,
                        "reason": f"unknown_predictor_type: {predictor_type}"}
        except Exception as e:
            return {"method": None, "computed_at": None, "top_features": [],
                    "leakage_warning": None, "full_path": None,
                    "reason": f"load_failed: {e}"}

        if fi is None:
            return {"method": None, "computed_at": None, "top_features": [],
                    "leakage_warning": None, "full_path": None,
                    "reason": reason or "unknown"}

        # Persist CSV.
        try:
            fi_csv.parent.mkdir(parents=True, exist_ok=True)
            fi.to_csv(fi_csv, index=False)
        except Exception:
            pass

        leakage = detect_leakage(fi)
        return {
            "method": "permutation",
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "top_features": to_jsonable(fi.head(TOP_FEATURES_N)),
            "leakage_warning": leakage,
            "full_path": str(fi_csv.relative_to(_config.ARTIFACTS_DIR.resolve())),
            "reason": None,
        }


# ---------------------------------------------------------------------------
# Report assembly + persistence
# ---------------------------------------------------------------------------


def _build_next_actions(report: dict) -> list[str]:
    actions: list[str] = []
    fi = report.get("feature_importance") or {}
    if fi and fi.get("leakage_warning"):
        actions.append(f"Review leakage warning: {fi['leakage_warning']}")
    proc = report.get("training_process") or {}
    if proc.get("time_limit_hit"):
        actions.append("Training hit the time limit; retrain with a larger time_limit or higher presets for better scores.")
    ptype = report.get("predictor_type")
    eval_summary = report.get("evaluation") or {}
    actions.append(f"Download leaderboard via download_urls.leaderboard_csv (metric={eval_summary.get('metric_name')}, direction={eval_summary.get('metric_direction')})")
    if ptype == "tabular":
        actions.append("Evaluate on a holdout dataset with evaluate_tabular.")
        actions.append("Score new data with predict_tabular.")
    elif ptype == "timeseries":
        actions.append("Forecast new data with predict_timeseries.")
    elif ptype == "multimodal":
        actions.append("Score new data with predict_multimodal.")
    # Dedupe preserving order.
    seen = set()
    out = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def assemble_report(*, predictor_type: str, task: Task, params: dict,
                    data_summary: dict, training_config_extra: dict | None,
                    predictor: Any, leaderboard: Any, fit_summary: dict | None,
                    duration_seconds: float) -> dict[str, Any]:
    """Build the full T-segment of the report (everything except S/L-injected fields)."""
    finished = time.time()
    training_config = {
        "presets": params.get("presets"),
        "eval_metric": None,
        "time_limit": params.get("time_limit"),
        "hyperparameters": params.get("hyperparameters"),
        "random_seed": params.get("random_seed"),
        "verbosity": 0,
        "autogluon_version": _autogluon_version(),
    }
    if training_config_extra:
        training_config.update(training_config_extra)

    # Metric + direction.
    metric_name = None
    metric_value = None
    metric_direction = None
    oof_metric_value = None
    best_model = None
    try:
        em = getattr(predictor, "eval_metric", None)
        if em is not None:
            metric_name = getattr(em, "name", str(em))
            gib = getattr(em, "greater_is_better", None)
            metric_direction = "higher_is_better" if gib else "lower_is_better" if gib is False else None
    except Exception:
        pass
    try:
        best_model = getattr(predictor, "model_best", None)
    except Exception:
        pass

    # leaderboard → metric_value + leaderboard_top
    lb_top = []
    if leaderboard is not None:
        try:
            lb_top = to_jsonable(leaderboard.head(5))
            # best_model row's score_val.
            if best_model and "model" in leaderboard.columns and "score_val" in leaderboard.columns:
                row = leaderboard[leaderboard["model"] == best_model]
                if not row.empty:
                    metric_value = float(row["score_val"].iloc[0])
        except Exception:
            pass

    # oof_metric_value from fit_summary.
    if fit_summary and best_model:
        mp = fit_summary.get("model_performance") or {}
        if best_model in mp:
            try:
                oof_metric_value = float(mp[best_model])
            except Exception:
                pass

    time_limit_hit, early_stopped = _detect_log_flags(
        task.task_id, duration_seconds, params.get("time_limit"))

    # training_process
    models_trained: list[str] = []
    models_attempted = 0
    if leaderboard is not None and "model" in leaderboard.columns:
        try:
            models_trained = [str(m) for m in leaderboard["model"].tolist()]
            models_attempted = len(models_trained)
        except Exception:
            pass

    report = {
        "schema_version": SCHEMA_VERSION,
        "model_id": params["model_id"],
        "task_id": task.task_id,
        "status": "success",
        "started_at": _iso(getattr(task, "started_at", None) or
                            (finished - duration_seconds)),
        "finished_at": _iso(finished),
        "duration_seconds": round(float(duration_seconds), 3),
        "predictor_type": predictor_type,
        "data_summary": data_summary,
        "training_config": training_config,
        "training_process": {
            "models_attempted": models_attempted,
            "models_trained": models_trained,
            "time_limit_hit": time_limit_hit,
            "early_stopped": early_stopped,
            "log_tail": _read_log_tail(task.task_id, LOG_TAIL_LINES),
        },
        "evaluation": {
            "best_model": str(best_model) if best_model else None,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "metric_direction": metric_direction,
            "oof_metric_value": oof_metric_value,
            "leaderboard_top": lb_top,
            "fit_summary": to_jsonable(fit_summary) if fit_summary else {},
        },
        "feature_importance": None,
        "artifacts": {
            "model_dir": str(model_path(params["model_id"]).relative_to(_config.ARTIFACTS_DIR.resolve())),
            "predictor_path": str(model_path(params["model_id"]).relative_to(_config.ARTIFACTS_DIR.resolve())),
            "leaderboard_csv": str((model_path(params["model_id"]) / "leaderboard.csv").relative_to(_config.ARTIFACTS_DIR.resolve())),
            "fit_summary_json": str((model_path(params["model_id"]) / "fit_summary.json").relative_to(_config.ARTIFACTS_DIR.resolve())),
            "feature_importance_csv": str((model_path(params["model_id"]) / "feature_importance.csv").relative_to(_config.ARTIFACTS_DIR.resolve())),
            "report_json": str((model_path(params["model_id"]) / "report.json").relative_to(_config.ARTIFACTS_DIR.resolve())),
            "training_log": str(log_path(task.task_id).relative_to(_config.ARTIFACTS_DIR.resolve())) if task.task_id else None,
        },
        "download_urls": {},  # S-injected, never persisted
        "next_actions": [],  # filled after FI computed/persisted
    }
    report["next_actions"] = _build_next_actions(report)
    return report


def persist_report(model_id: str, report: dict) -> str:
    """Write report.json (without download_urls). Returns the relative path string."""
    validate_id(model_id, "model_id")
    # Strip download_urls before persisting — they are per-request presigned.
    persistable = {k: v for k, v in report.items() if k != "download_urls"}
    out = model_path(model_id) / "report.json"
    _json_atomic_write(out, persistable)
    return str(out.relative_to(_config.ARTIFACTS_DIR.resolve()))


def load_report(model_id: str) -> dict | None:
    """Read report.json. Returns None if missing."""
    validate_id(model_id, "model_id")
    p = model_path(model_id) / "report.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# download_urls injection (S-segment)
# ---------------------------------------------------------------------------


def build_download_urls(artifacts: dict[str, str | None],
                         agent_id: str,
                         ttl_seconds: int = MCP_DOWNLOAD_URL_TTL_SECONDS) -> dict[str, str]:
    """Build presigned URLs for every path in ``artifacts``. Returns {} if
    base URL or signing key unconfigured."""
    if not MCP_ARTIFACT_BASE_URL:
        return {}
    urls: dict[str, str] = {}
    expires_at = int(time.time()) + ttl_seconds
    for key, rel_path in artifacts.items():
        if not rel_path:
            continue
        token = build_presigned_token(rel_path, expires_at, agent_id)
        if token is None:
            continue
        from urllib.parse import quote
        urls[key] = f"{MCP_ARTIFACT_BASE_URL}/download?token={token}&path={quote(rel_path, safe='')}"
    # Virtual model archive (whole model dir as tar.gz).
    model_dir = artifacts.get("model_dir")
    if model_dir:
        archive_path = f"{model_dir}.tar.gz"
        token = build_presigned_token(archive_path, expires_at, agent_id)
        if token is not None:
            from urllib.parse import quote
            urls["model_archive"] = f"{MCP_ARTIFACT_BASE_URL}/download?token={token}&path={quote(archive_path, safe='')}"
    return urls


def _current_agent_id() -> str | None:
    try:
        from tools.auth import current_agent_id
        v = current_agent_id.get()
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public tool: get_training_report
# ---------------------------------------------------------------------------


def _get_training_report(model_id: str, include_feature_importance: bool = False,
                          leaderboard_top_n: int = 0) -> dict[str, Any]:
    validate_id(model_id, "model_id")
    report = load_report(model_id)
    if report is None:
        entry = get_registry_entry(model_id)
        if entry is None:
            raise FileNotFoundError(
                f"report.json not found for model {model_id!r} "
                "(model not in registry; check model_id or train first)")
        raise FileNotFoundError(
            f"report.json not found for model {model_id!r} "
            "(trained before v0.6.0); retrain to generate a report")

    # Optional leaderboard re-slice.
    if leaderboard_top_n and leaderboard_top_n > 0:
        lb_csv = model_path(model_id) / "leaderboard.csv"
        if lb_csv.exists():
            try:
                lb = pd.read_csv(lb_csv)
                report.setdefault("evaluation", {})["leaderboard_top"] = to_jsonable(lb.head(leaderboard_top_n))
            except Exception:
                pass

    # Lazy feature_importance.
    if include_feature_importance and not report.get("feature_importance"):
        entry = get_registry_entry(model_id) or {}
        ptype = entry.get("type") or report.get("predictor_type") or "tabular"
        dataset_id = entry.get("dataset_id") or (report.get("data_summary") or {}).get("dataset_id")
        target = entry.get("target") or (report.get("data_summary") or {}).get("target")
        fi = compute_feature_importance(model_id, ptype, dataset_id, target)
        report["feature_importance"] = fi
        # Rebuild next_actions with leakage warning if present.
        report["next_actions"] = _build_next_actions(report)
        # Persist the updated FI section.
        persistable = {k: v for k, v in report.items() if k != "download_urls"}
        try:
            _json_atomic_write(model_path(model_id) / "report.json", persistable)
        except Exception:
            pass

    # Inject download_urls (never persisted).
    agent_id = _current_agent_id() or "anonymous"
    report["download_urls"] = build_download_urls(report.get("artifacts") or {}, agent_id)
    return report


def get_training_report(
    model_id: str,
    include_feature_importance: bool = False,
    leaderboard_top_n: int = 0,
) -> dict[str, Any]:
    """Return the full training report for a completed model.

    Reads ``artifacts/models/<model_id>/report.json`` written at train time.
    When ``include_feature_importance=true`` and the FI CSV is missing, it is
    computed on-demand by reloading the predictor (a few seconds) and cached.
    ``leaderboard_top_n>0`` re-slices the leaderboard (default 5).
    """
    return envelope_call(_get_training_report, model_id, include_feature_importance, leaderboard_top_n)


get_training_report = safe_tool(get_training_report)
