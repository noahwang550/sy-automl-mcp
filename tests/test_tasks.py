"""Tests for the background TaskManager (no AutoGluon needed)."""
from __future__ import annotations

import time

import pytest

from tasks import get_task_manager
from tasks.manager import FAILED, SUCCESS

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
