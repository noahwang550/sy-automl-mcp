"""Shared helpers for MCP tools.

This module is intentionally small — tools are thin wrappers around
AutoGluon calls. The ``envelope_call`` helper lets public tools return the
unified ``{success, data, error}`` envelope while keeping function bodies
readable.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable
from typing import Any

from serialization import failure, success


class _ThreadLocalOutputProxy:
    """Thread-local dispatch proxy for stdout/stderr.

    Installed once as ``sys.stdout`` / ``sys.stderr``. Each thread can set its
    own target (e.g. a task log file or ``os.devnull``) via
    :meth:`set_target`. Writes from one thread never leak into another thread's
    target, and the global ``sys.stdout`` / ``sys.stderr`` objects are never
    swapped, which keeps the stdio JSON-RPC stream safe under concurrency.
    """

    __slots__ = ("_real", "_lock", "_targets")

    def __init__(self, real_stream: Any) -> None:
        self._real = real_stream
        self._lock = threading.Lock()
        self._targets: dict[int, Any] = {}

    def _target(self) -> Any:
        with self._lock:
            return self._targets.get(threading.current_thread().ident, self._real)

    def set_target(self, target: Any) -> None:
        with self._lock:
            self._targets[threading.current_thread().ident] = target

    def get_target(self) -> Any:
        return self._target()

    def reset(self) -> None:
        with self._lock:
            self._targets.pop(threading.current_thread().ident, None)

    def write(self, s: str) -> int:  # noqa: D102
        return self._target().write(s)

    def flush(self) -> None:  # noqa: D102
        return self._target().flush()

    def writelines(self, lines: list[str]) -> None:  # noqa: D102
        return self._target().writelines(lines)

    def fileno(self) -> int:  # noqa: D102
        return self._target().fileno()

    def isatty(self) -> bool:  # noqa: D102
        return self._target().isatty()

    def __iter__(self):  # noqa: D102
        return iter(self._target())

    def __next__(self):  # noqa: D102
        return next(self._target())

    def __enter__(self) -> Any:  # noqa: D102
        return self._target().__enter__()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:  # noqa: D102
        return self._target().__exit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name: str) -> Any:  # noqa: D105
        return getattr(self._target(), name)


def _install_thread_local_output_proxy() -> tuple[_ThreadLocalOutputProxy, _ThreadLocalOutputProxy]:
    """Install the thread-local proxy once and only once."""
    if isinstance(sys.stdout, _ThreadLocalOutputProxy) and isinstance(sys.stderr, _ThreadLocalOutputProxy):
        # Already installed. Return the existing proxies (they are sys.stdout/ stderr themselves).
        return sys.stdout, sys.stderr  # type: ignore[return-value]
    stdout_proxy = _ThreadLocalOutputProxy(sys.stdout)
    stderr_proxy = _ThreadLocalOutputProxy(sys.stderr)
    sys.stdout = stdout_proxy  # type: ignore[assignment]
    sys.stderr = stderr_proxy  # type: ignore[assignment]
    return stdout_proxy, stderr_proxy


_stdout_proxy, _stderr_proxy = _install_thread_local_output_proxy()


def set_thread_output_target(target: Any) -> None:
    """Redirect this thread's stdout/stderr to *target*."""
    _stdout_proxy.set_target(target)
    _stderr_proxy.set_target(target)


def reset_thread_output_target() -> None:
    """Restore this thread's stdout/stderr to the real streams."""
    _stdout_proxy.reset()
    _stderr_proxy.reset()


@contextlib.contextmanager
def _suppress_output():
    """Redirect stdout and stderr to the OS null device for the current thread.

    AutoGluon and its transitive dependencies may write progress output to
    stdout/stderr. In stdio MCP mode that would corrupt the JSON-RPC stream,
    so any output produced inside this context is discarded.
    """
    with open(os.devnull, "w") as devnull:
        old_stdout_target = _stdout_proxy.get_target()
        old_stderr_target = _stderr_proxy.get_target()
        _stdout_proxy.set_target(devnull)
        _stderr_proxy.set_target(devnull)
        try:
            yield
        finally:
            _stdout_proxy.set_target(old_stdout_target)
            _stderr_proxy.set_target(old_stderr_target)


def envelope_call(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Call ``fn`` and wrap its result or any exception in the unified envelope.

    Tool return values are preserved; any stdout/stderr emitted by the
    wrapped function is suppressed to keep the stdio protocol clean.
    """
    with _suppress_output():
        try:
            return success(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 (MCP tools must not leak exceptions)
            return failure(exc)
