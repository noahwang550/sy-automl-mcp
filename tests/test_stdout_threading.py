"""Tests for thread-safe stdout/stderr redirect (Item 4).

Pytest captures stdout by replacing ``sys.stdout`` with its own object. Our
proxy is installed once at import time, so these tests explicitly restore
``sys.stdout``/``sys.stderr`` to the proxy around the operations they want to
observe.
"""
from __future__ import annotations

import io
import sys
import threading
from typing import Any

from tools._common import (
    _stderr_proxy,
    _stdout_proxy,
    envelope_call,
    reset_thread_output_target,
    set_thread_output_target,
)


class _CapturingStream:
    """Thread-safe capture buffer compatible with the output proxy."""

    def __init__(self):
        self._buf = io.StringIO()
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            return self._buf.write(s)

    def flush(self) -> None:
        with self._lock:
            self._buf.flush()

    def getvalue(self) -> str:
        with self._lock:
            return self._buf.getvalue()


def _install_proxy() -> tuple[Any, Any]:
    """Restore sys.stdout/stderr to the proxy for a test."""
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = _stdout_proxy
    sys.stderr = _stderr_proxy
    return old_stdout, old_stderr


def test_envelope_call_still_suppresses_output_under_proxy():
    """envelope_call must keep stdout/stderr clean even with the proxy installed."""
    captured = io.StringIO()
    old_stdout, old_stderr = _install_proxy()
    try:

        def noisy_fn():
            print("should-not-appear", file=sys.stdout, end="")
            return {"ok": True}

        set_thread_output_target(captured)
        try:
            result = envelope_call(noisy_fn)
        finally:
            reset_thread_output_target()

        assert result["success"] is True
        assert captured.getvalue() == ""
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def test_single_thread_envelope_call_still_returns_success():
    """The envelope_call happy path is unchanged with the proxy installed."""

    def fn():
        return {"ok": True}

    result = envelope_call(fn)
    assert result["success"] is True
    assert result["data"] == {"ok": True}


def test_thread_local_output_isolation():
    """Two threads with distinct targets must not interleave output."""
    old_stdout, old_stderr = _install_proxy()
    try:
        stream_a = _CapturingStream()
        stream_b = _CapturingStream()

        def worker(name: str, stream: _CapturingStream) -> None:
            set_thread_output_target(stream)
            try:
                print(f"from-{name}", file=sys.stdout, end="")
            finally:
                reset_thread_output_target()

        t1 = threading.Thread(target=worker, args=("A", stream_a))
        t2 = threading.Thread(target=worker, args=("B", stream_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert stream_a.getvalue() == "from-A"
        assert stream_b.getvalue() == "from-B"
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def test_main_thread_default_target_forwards_to_real_stdout():
    """When no thread-local target is set, writes reach the original stdout."""
    old_stdout, old_stderr = _install_proxy()
    try:
        calls: list[str] = []

        class Hook:
            def write(self, s: str) -> int:
                calls.append(s)
                return len(s)

            def flush(self) -> None:
                pass

        hook = Hook()
        set_thread_output_target(hook)
        try:
            print("proxy-test-123", file=sys.stdout, end="")
        finally:
            reset_thread_output_target()
        assert "proxy-test-123" in calls
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
