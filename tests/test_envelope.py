"""Tests for the unified response envelope (no AutoGluon required)."""
from __future__ import annotations

from serialization import failure, success


def test_success_envelope():
    out = success({"task_id": "abc"})
    assert out == {"success": True, "data": {"task_id": "abc"}, "error": None}


def test_failure_envelope_with_message():
    out = failure("boom", data=None)
    assert out["success"] is False
    assert out["error"] == "boom"
    assert out["data"] is None


def test_failure_envelope_with_exception():
    exc = ValueError("bad input")
    out = failure(exc, data={"field": "x"})
    assert out["success"] is False
    assert out["error"] == "bad input"
    assert out["data"] == {"field": "x"}
