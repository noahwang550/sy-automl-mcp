"""TaskManager — submit, query, and (soft-)cancel background AutoGluon jobs.

AutoGluon's API is synchronous and blocking; a single ``fit`` can run for
minutes or hours. MCP tool calls must not block that long, so training tools
submit work to a :class:`TaskManager` and return a ``task_id`` immediately.

Design notes:
- Uses :class:`ThreadPoolExecutor` (not asyncio) because AutoGluon is sync;
  wrapping every call in ``run_in_executor`` adds complexity for no gain.
- Default ``max_workers=1`` (serial) — AutoGluon already saturates a machine
  and parallel training risks OOM. Configurable via ``MCP_MAX_WORKERS``.
- Cancellation is **soft**: Python cannot safely kill a thread. We set a flag
  and rely on AutoGluon's ``time_limit`` to bound runtime. Callers should
  always set ``time_limit`` on training jobs.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from config import MCP_MAX_WORKERS, ensure_dirs, log_path

from .registry import FAILED, SUCCESS, Task, TaskStore, _TERMINAL

__all__ = ["Task", "TaskManager", "get_task_manager"]


class TaskManager:
    """Singleton managing the executor and task store."""

    def __init__(self, max_workers: int = MCP_MAX_WORKERS) -> None:
        ensure_dirs()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="automl")
        self.store = TaskStore()
        self._lock = threading.Lock()

    # -- submission ---------------------------------------------------------

    def submit(
        self,
        task_type: str,
        func: Callable[[Task], dict[str, Any] | None],
        params: dict[str, Any],
        log_filename: str | None = None,
    ) -> str:
        """Submit ``func`` (which receives the Task) and return its task_id.

        ``func`` returns an optional result-summary dict. Its exception, if any,
        is captured into ``Task.error`` and status set to FAILED.
        """
        task_id = uuid.uuid4().hex
        log_file = log_path(log_filename) if log_filename else log_path(task_id)
        task = Task(
            task_id=task_id,
            type=task_type,
            params=params,
            log_path=str(log_file),
        )
        self.store.add(task)
        self._executor.submit(self._run, task, func)
        return task_id

    def _run(self, task: Task, func: Callable[[Task], dict[str, Any] | None]) -> None:
        logf = None
        try:
            logf = open(task.log_path, "a", encoding="utf-8")  # noqa: SIM115
        except OSError:
            pass

        def _log(msg: str) -> None:
            line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
            if logf:
                try:
                    logf.write(line + "\n")
                    logf.flush()
                except OSError:
                    pass

        with self._lock:
            if task.is_cancelled():
                task.status = "cancelled"
                task.finished_at = time.time()
                return
            task.status = "running"
            task.started_at = time.time()
        _log(f"START {task.type} params={task.params}")

        try:
            if task.is_cancelled():
                task.status = "cancelled"
                _log("CANCELLED before execution")
                return
            # Redirect any stdout/stderr produced by AutoGluon to the task log
            # so the stdio protocol stays clean while preserving diagnostics.
            log_target = logf if logf is not None else open(os.devnull, "w")
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = log_target
            sys.stderr = log_target
            try:
                summary = func(task)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                if logf is None:
                    log_target.close()
            if task.is_cancelled():
                task.status = "cancelled"
                _log("CANCELLED during execution")
            else:
                task.result_summary = summary or {}
                task.status = SUCCESS
                _log(f"SUCCESS summary={task.result_summary}")
        except Exception as exc:  # capture everything; MCP must not crash
            task.error = f"{type(exc).__name__}: {exc}"
            task.status = FAILED
            _log(f"FAILED {task.error}\n{traceback.format_exc()}")
        finally:
            task.finished_at = time.time()
            if logf:
                try:
                    logf.close()
                except OSError:
                    pass

    # -- query / control ----------------------------------------------------

    def get(self, task_id: str) -> Task:
        return self.store.require(task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        return self.store.require(task_id).to_dict()

    def result(self, task_id: str) -> dict[str, Any]:
        task = self.store.require(task_id)
        d = task.to_dict(include_log_tail=0)
        if task.status not in _TERMINAL:
            d["note"] = "Task not finished; poll get_task_status."
        return d

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.require(task_id)
        ok = task.request_cancel()
        return {
            "task_id": task_id,
            "status": task.status,
            "cancellation_requested": ok,
            "note": (
                "Soft cancel requested. AutoGluon cannot be hard-killed; the "
                "job will stop at the next time_limit boundary. Always set "
                "time_limit on training jobs."
            ),
        }

    def list(self) -> list[dict[str, Any]]:
        return [t.to_dict(include_log_tail=0) for t in self.store.list()]


_SINGLETON: TaskManager | None = None
_SINGLETON_LOCK = threading.Lock()


def get_task_manager() -> TaskManager:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = TaskManager()
    return _SINGLETON


def shutdown() -> None:
    global _SINGLETON
    if _SINGLETON is not None:
        _SINGLETON._executor.shutdown(wait=False, cancel_futures=False)
        _SINGLETON = None
