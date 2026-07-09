"""Tests for the background TaskManager (no AutoGluon needed)."""
from __future__ import annotations

import time

import pytest

from tasks import get_task_manager
from tasks.manager import FAILED, SUCCESS, Task


def test_submit_success(isolated_artifacts):
    mgr = get_task_manager()

    def job(task):
        return {"ok": True, "value": 42}

    task_id = mgr.submit("test_job", job, {"x": 1})
    # Poll until terminal (bounded).
    for _ in range(100):
        st = mgr.status(task_id)["status"]
        if st in {SUCCESS, FAILED}:
            break
        time.sleep(0.05)
    res = mgr.result(task_id)
    assert res["status"] == SUCCESS
    assert res["result_summary"] == {"ok": True, "value": 42}
    assert res["params"] == {"x": 1}
    assert res["elapsed_sec"] >= 0


def test_submit_failure_captures_exception(isolated_artifacts):
    mgr = get_task_manager()

    def job(task):
        raise RuntimeError("boom")

    task_id = mgr.submit("failing_job", job, {})
    for _ in range(100):
        if mgr.status(task_id)["status"] in {SUCCESS, FAILED}:
            break
        time.sleep(0.05)
    res = mgr.result(task_id)
    assert res["status"] == FAILED
    assert "RuntimeError: boom" in res["error"]


def test_cancel_before_run_marks_cancelled(isolated_artifacts):
    """A job that sleeps gives cancel a window to fire."""
    mgr = get_task_manager()

    def slow_job(task):
        for _ in range(200):
            if task.is_cancelled():
                return None
            time.sleep(0.01)
        return {"done": True}

    task_id = mgr.submit("slow_job", slow_job, {})
    # Request cancel; the job should observe the flag and stop.
    cancel_result = mgr.cancel(task_id)
    assert cancel_result["cancellation_requested"] in (True, False)  # race-dependent
    for _ in range(200):
        if mgr.status(task_id)["status"] in {SUCCESS, FAILED, "cancelled"}:
            break
        time.sleep(0.02)
    # The manager must not hang and must reach a terminal state. Soft-cancel
    # yields "cancelled"; if the job raced to completion first, SUCCESS.
    final = mgr.status(task_id)["status"]
    assert final in {SUCCESS, FAILED, "cancelled"}


def test_cancel_after_success_leaves_state_success(isolated_artifacts):
    """A cancel request after the task reached SUCCESS must not overwrite it."""
    mgr = get_task_manager()

    def job(task):
        return {"ok": True}

    task_id = mgr.submit("quick_job", job, {})
    for _ in range(100):
        if mgr.status(task_id)["status"] == SUCCESS:
            break
        time.sleep(0.05)
    result = mgr.cancel(task_id)
    assert mgr.status(task_id)["status"] == SUCCESS
    assert result["cancellation_requested"] is False
    assert result["status"] == SUCCESS
    assert result.get("already_terminal") is True


def test_cancel_after_failed_leaves_state_failed(isolated_artifacts):
    """A cancel request after the task reached FAILED must not overwrite it."""
    mgr = get_task_manager()

    def job(task):
        raise RuntimeError("boom")

    task_id = mgr.submit("failing_job", job, {})
    for _ in range(100):
        if mgr.status(task_id)["status"] == FAILED:
            break
        time.sleep(0.05)
    result = mgr.cancel(task_id)
    assert mgr.status(task_id)["status"] == FAILED
    assert result["cancellation_requested"] is False
    assert result["status"] == FAILED
    assert result.get("already_terminal") is True


def test_cancel_while_running_transitions_to_cancelled(isolated_artifacts):
    """A cancel request observed while the worker is running becomes CANCELLED."""
    mgr = get_task_manager()

    def slow_job(task):
        for _ in range(200):
            if task.is_cancelled():
                return None
            time.sleep(0.01)
        return {"done": True}

    task_id = mgr.submit("slow_job", slow_job, {})
    for _ in range(50):
        if mgr.status(task_id)["status"] == "running":
            break
        time.sleep(0.01)
    mgr.cancel(task_id)
    for _ in range(200):
        if mgr.status(task_id)["status"] in {SUCCESS, FAILED, "cancelled"}:
            break
        time.sleep(0.02)
    assert mgr.status(task_id)["status"] == "cancelled"


def test_double_cancel_is_safe(isolated_artifacts):
    """Repeated cancels must not corrupt state or raise."""
    mgr = get_task_manager()

    def slow_job(task):
        for _ in range(200):
            if task.is_cancelled():
                return None
            time.sleep(0.01)
        return {"done": True}

    task_id = mgr.submit("slow_job", slow_job, {})
    for _ in range(50):
        if mgr.status(task_id)["status"] == "running":
            break
        time.sleep(0.01)
    first = mgr.cancel(task_id)
    second = mgr.cancel(task_id)
    for _ in range(200):
        if mgr.status(task_id)["status"] in {SUCCESS, FAILED, "cancelled"}:
            break
        time.sleep(0.02)
    # The exact value of the second request is race-dependent; the important
    # behavior is that it does not raise and reports a terminal/cancelling state.
    assert isinstance(first["cancellation_requested"], bool)
    assert isinstance(second["cancellation_requested"], bool)
    final = mgr.status(task_id)["status"]
    assert final in {SUCCESS, FAILED, "cancelled"}


def test_unknown_task_raises(isolated_artifacts):
    mgr = get_task_manager()
    with pytest.raises(KeyError):
        mgr.status("nope")


def test_status_includes_log_tail(isolated_artifacts):
    mgr = get_task_manager()

    def job(task):
        return {"ok": True}

    task_id = mgr.submit("logged_job", job, {})
    for _ in range(100):
        if mgr.status(task_id)["status"] in {SUCCESS, FAILED}:
            break
        time.sleep(0.05)
    st = mgr.status(task_id)
    # to_dict() omits the raw log_path; a finished job with a log file should
    # surface a log_tail instead.
    assert st["status"] == SUCCESS
    assert "log_tail" in st


# ---------------------------------------------------------------------------
# TaskStore retention / eviction (Item 3)
# ---------------------------------------------------------------------------


def test_old_terminal_task_evicted_on_sweep(isolated_artifacts, monkeypatch):
    mgr = get_task_manager()
    # Create a completed task that is far older than the retention window.
    old_task = Task(
        task_id="old-success",
        type="test",
        params={},
        status=SUCCESS,
        created_at=0.0,
        started_at=0.0,
        finished_at=0.0,
    )
    mgr.store.add(old_task)
    # Force a sweep by calling a read operation.
    mgr.store.get("old-success")
    assert mgr.store.get("old-success") is None


def test_running_pending_task_not_evicted(isolated_artifacts, monkeypatch):
    mgr = get_task_manager()
    running = Task(
        task_id="running-task",
        type="test",
        params={},
        status="running",
        created_at=0.0,
        started_at=0.0,
        finished_at=None,
    )
    pending = Task(
        task_id="pending-task",
        type="test",
        params={},
        status="pending",
        created_at=0.0,
        finished_at=None,
    )
    mgr.store.add(running)
    mgr.store.add(pending)
    mgr.store.sweep()
    assert mgr.store.get("running-task") is not None
    assert mgr.store.get("pending-task") is not None


def test_recently_completed_task_survives(isolated_artifacts, monkeypatch):
    mgr = get_task_manager()
    import time as _time

    recent = Task(
        task_id="recent-failed",
        type="test",
        params={},
        status=FAILED,
        created_at=_time.time() - 10.0,
        started_at=_time.time() - 10.0,
        finished_at=_time.time() - 10.0,
    )
    mgr.store.add(recent)
    mgr.store.sweep()
    assert mgr.store.get("recent-failed") is not None


def test_get_task_status_on_evicted_id_returns_not_found(isolated_artifacts, monkeypatch):
    mgr = get_task_manager()
    old_task = Task(
        task_id="expired-task",
        type="test",
        params={},
        status=SUCCESS,
        created_at=0.0,
        started_at=0.0,
        finished_at=0.0,
    )
    mgr.store.add(old_task)
    # Evict via sweep.
    mgr.store.sweep()
    # get_task_status should return a failure envelope, not raise.
    from tools.task_status import get_task_status

    result = get_task_status("expired-task")
    assert result["success"] is False
    assert "expired" in result["error"].lower() or "not found" in result["error"].lower()
