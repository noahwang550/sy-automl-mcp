"""Best-effort AutoGluon task-log progress parsing.

AutoGluon's ``fit()`` writes human-readable progress to stdout/stderr, which
the :class:`TaskManager` redirects into the per-task log file (see
``tasks/manager.py``). This module turns the tail of that log into a small
structured dict surfaced via ``get_task_status`` so callers can see *training
progress* (models attempted, latest validation score, recent log lines) without
having to eyeball the raw log.

Design notes:
- Pure-function, dependency-free, and cheap: it reads the (small) log file once
  and scans with compiled regexes. Never imports AutoGluon.
- Honest about uncertainty: it reports the *latest* validation score/model seen,
  not a claimed "best" — AutoGluon's internal ``score_val`` is oriented so
  higher-is-better on the leaderboard, but the raw "Validation score" lines
  print the native metric value whose direction (higher/lower is better)
  depends on the metric, so we do not guess.
- Robust to missing/garbled logs: returns ``{"available": False, ...}`` rather
  than raising, so ``get_task_status`` never fails because the log is odd.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["parse_progress"]


# "Fitting N model(s) ..." — AutoGluon announces how many models it will try.
_FIT_COUNT_RE = re.compile(r"Fitting\s+(\d+)\s+model", re.IGNORECASE)

# "Fitting model: RandomForestGini ..." — one line per attempted model.
_FIT_MODEL_RE = re.compile(r"Fitting model:\s*(\S+)")

# AutoGluon validation-score line, e.g.:
#   "  0.8531  = Validation score (accuracy) | RandomForestGini"
# Captures score, metric name, and model name.
_SCORE_RE = re.compile(
    r"([0-9]*\.?[0-9]+)\s*=\s*Validation score \(([^)]+)\)\s*\|\s*(.+?)\s*$"
)

# How many trailing non-blank log lines to surface as "recent_lines".
_RECENT_LINE_COUNT = 8


def _read_log_lines(log_path: str | None) -> list[str] | None:
    """Return log lines (newline-stripped) or None if unreadable/absent."""
    if not log_path:
        return None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()]
    except OSError:
        return None


def parse_progress(log_path: str | None, status: str) -> dict[str, Any]:
    """Parse an AutoGluon task log into a structured progress snapshot.

    Args:
        log_path: path to the per-task log file (may be None or missing).
        status: the task's current status string (echoed back for convenience).

    Returns a dict that is always JSON-serializable. When the log is absent or
    unreadable, ``{"available": False, ...}`` is returned (never raises).
    """
    lines = _read_log_lines(log_path)
    if lines is None:
        return {"available": False, "reason": "log not available", "status": status}

    announced_models: int | None = None
    models_attempted = 0
    latest_score: float | None = None
    latest_model: str | None = None
    metric: str | None = None

    for line in lines:
        count_match = _FIT_COUNT_RE.search(line)
        if count_match and announced_models is None:
            try:
                announced_models = int(count_match.group(1))
            except ValueError:
                pass
        if _FIT_MODEL_RE.search(line):
            models_attempted += 1
        score_match = _SCORE_RE.search(line)
        if score_match:
            try:
                latest_score = float(score_match.group(1))
            except ValueError:
                continue
            metric = score_match.group(2)
            latest_model = score_match.group(3)

    recent_lines = [ln for ln in lines if ln.strip()][-_RECENT_LINE_COUNT:]

    return {
        "available": True,
        "status": status,
        "announced_models": announced_models,
        "models_attempted": models_attempted,
        "latest_score": latest_score,
        "latest_model": latest_model,
        "metric": metric,
        "recent_lines": recent_lines,
    }
