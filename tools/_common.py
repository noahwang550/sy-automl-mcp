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
from typing import Any, Callable

from serialization import failure, success


@contextlib.contextmanager
def _suppress_output():
    """Redirect stdout and stderr to the OS null device.

    AutoGluon and its transitive dependencies may write progress output to
    stdout/stderr. In stdio MCP mode that would corrupt the JSON-RPC stream,
    so any output produced inside this context is discarded.
    """
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


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
