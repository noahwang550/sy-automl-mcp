"""Task dataclass + in-memory store (task_id -> Task), lock-protected.

Kept dependency-free (no import from ``manager``) to avoid a circular import:
``manager`` imports :class:`TaskStore`/ :class:`Task` from here.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import MCP_TASK_MAX_RETAINED, MCP_TASK_RETENTION_SECONDS

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
        with open(path, encoding="utf-8", errors="replace") as f:
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
    started_at: float | None = None
    finished_at: float | None = None
    result_summary: dict[str, Any] | None = None
    error: str | None = None
    artifact_path: str | None = None
    log_path: str | None = None
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- cancellation -------------------------------------------------------

    def request_cancel(self) -> bool:
        with self._state_lock:
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
    """Thread-safe map of task_id -> Task with lazy retention sweep."""

    def __init__(
        self,
        retention_seconds: float | None = None,
        max_retained: int | None = None,
    ) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self._retention_seconds = (
            retention_seconds if retention_seconds is not None else MCP_TASK_RETENTION_SECONDS
        )
        self._max_retained = max_retained if max_retained is not None else MCP_TASK_MAX_RETAINED

    def _sweep_locked(self, now: float) -> None:
        """Evict old terminal tasks. Must be called with ``_lock`` held."""
        cutoff = now - self._retention_seconds
        # First pass: remove tasks older than the retention window.
        expired_ids = [
            task_id
            for task_id, task in self._tasks.items()
            if task.status in _TERMINAL
            and task.finished_at is not None
            and task.finished_at < cutoff
        ]
        for task_id in expired_ids:
            del self._tasks[task_id]
        # Second pass: cap the number of retained terminal tasks.
        terminal = [(tid, t) for tid, t in self._tasks.items() if t.status in _TERMINAL]
        if len(terminal) > self._max_retained:
            terminal_sorted = sorted(terminal, key=lambda item: item[1].finished_at or 0)
            for task_id, _ in terminal_sorted[: len(terminal) - self._max_retained]:
                del self._tasks[task_id]

    def sweep(self) -> None:
        """Public sweep entry point; acquires the store lock."""
        with self._lock:
            self._sweep_locked(time.time())

    def add(self, task: Task) -> None:
        with self._lock:
            self._tasks[task.task_id] = task
        self.sweep()

    def get(self, task_id: str) -> Task | None:
        self.sweep()
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        self.sweep()
        with self._lock:
            return list(self._tasks.values())

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def require(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"Task expired or not found: {task_id}")
        return task

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        task = self.get(task_id)
        return task.to_dict() if task else None
