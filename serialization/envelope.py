"""Unified response envelope for all MCP tools.

Every tool returns a JSON-safe dict with three keys:

    {
      "success": bool,
      "data": <payload on success>,
      "error": <error message on failure, otherwise null>
    }

Public tools wrap their work in ``success`` / ``failure`` so the MCP layer always
receives a well-formed response instead of an uncaught exception.
"""
from __future__ import annotations

from typing import Any


def success(data: Any) -> dict[str, Any]:
    """Return a successful envelope wrapping ``data``."""
    return {"success": True, "data": data, "error": None}


def failure(error: Exception | str, data: Any | None = None) -> dict[str, Any]:
    """Return a failed envelope with a stringified error and optional data."""
    return {"success": False, "data": data, "error": str(error)}
