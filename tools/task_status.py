"""Task status / result / cancel tools."""
from __future__ import annotations

from typing import Any

from tasks import get_task_manager

from ._common import envelope_call


def get_task_status(task_id: str) -> dict[str, Any]:
    """Return current status, elapsed time, and a log tail for a background task."""
    return envelope_call(get_task_manager().status, task_id)


def get_task_result(task_id: str) -> dict[str, Any]:
    """Return the result summary of a finished task.

    If the task is not yet terminal, returns its current status with a note to
    keep polling ``get_task_status``.
    """
    return envelope_call(get_task_manager().result, task_id)


def cancel_task(task_id: str) -> dict[str, Any]:
    """Soft-cancel a background task.

    AutoGluon cannot be hard-killed mid-fit; cancellation takes effect at the
    next ``time_limit`` boundary. Always set ``time_limit`` on training jobs.
    """
    return envelope_call(get_task_manager().cancel, task_id)


def list_tasks() -> dict[str, Any]:
    """List all known background tasks (most recent last)."""
    return envelope_call(get_task_manager().list)
