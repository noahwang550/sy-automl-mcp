"""Task dataclass + in-memory store (task_id -> Task), lock-protected.

Kept dependency-free (no import from ``manager``) to avoid a circular import:
``manager`` imports :class:`TaskStore`/ :class:`Task` from here.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Task lifecycle states. Cancel is terminal only after the worker observes it.
PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
CANCELLED = "cancelled"
CANCELLING = "cancelling"

_TERMINAL = {SUCCESS, FAILED, CANCELLED}


def _tail(path: str, n: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except OSError:
        return []


@dataclass
class Task:
    task_id: str
    type: str
    params: dict[str, Any]
    status: str = PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result_summary: dict[str, Any] | None = None
    error: str | None = None
    artifact_path: str | None = None
    log_path: str | None = None
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    # -- cancellation -------------------------------------------------------

    def request_cancel(self) -> bool:
        if self.status in _TERMINAL:
            return False
        self.status = CANCELLING
        self._cancel_event.set()
        return True

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # -- serialization ------------------------------------------------------

    def to_dict(self, include_log_tail: int = 20) -> dict[str, Any]:
        now = time.time()
        started = self.started_at or self.created_at
        elapsed = (self.finished_at or now) - started
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at,
            "elapsed_sec": round(elapsed, 1),
            "params": self.params,
            "result_summary": self.result_summary,
            "error": self.error,
            "artifact_path": self.artifact_path,
        }
        if include_log_tail and self.log_path and Path(self.log_path).exists():
            d["log_tail"] = _tail(self.log_path, include_log_tail)
        return d


class TaskStore:
    """Thread-safe map of task_id -> Task."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def add(self, task: Task) -> None:
        with self._lock:
            self._tasks[task.task_id] = task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def require(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        return task

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        task = self.get(task_id)
        return task.to_dict() if task else None
