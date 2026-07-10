"""Authentication helpers for the streamable-http MCP transport.

``check_bearer_token`` validates an ``Authorization`` (or ``X-API-Key``) header
against a configured token using a constant-time comparison.

``BearerTokenMiddleware`` protects the Starlette app produced by
:meth:`mcp.server.fastmcp.FastMCP.streamable_http_app`.  When the token is
unset the middleware is a no-op, preserving the existing unauthenticated
behavior.
"""
from __future__ import annotations

import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger("sy-automl-mcp")


def check_bearer_token(auth_header: str | None, expected: str | None) -> bool:
    """Return ``True`` if *auth_header* matches *expected*.

    Rules:
    * If ``expected`` is ``None`` (auth disabled) the check always passes.
    * ``Authorization: Bearer <token>`` is accepted (scheme is case-insensitive).
    * ``Authorization: X-API-Key <token>`` is accepted as a convenience.
    * A bare ``<token>`` is also accepted.
    * ``secrets.compare_digest`` is used for constant-time comparison.
    """
    if expected is None:
        return True

    if not isinstance(auth_header, str):
        return False

    auth_header = auth_header.strip()
    if not auth_header:
        return False

    parts = auth_header.split(None, 1)
    if len(parts) == 2:
        scheme, token = parts[0].lower(), parts[1]
        if scheme in {"bearer", "x-api-key"}:
            candidate = token
        else:
            # Unknown scheme; compare the whole header to avoid accidental matches.
            candidate = auth_header
    else:
        candidate = auth_header

    return secrets.compare_digest(candidate, expected)


def _extract_provided_token(request: Request) -> str | None:
    """Pull an authentication token from the request headers."""
    return (
        request.headers.get("Authorization")
        or request.headers.get("X-API-Key")
    )


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that gates every non-health request with a token."""

    def __init__(
        self,
        app,
        expected_token: str | None,
        exempt_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.expected_token = expected_token
        self.exempt_paths: set[str] = exempt_paths or {"/", "/health"}

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if self.expected_token is None:
            return await call_next(request)

        if request.method == "GET" and request.url.path in self.exempt_paths:
            return await call_next(request)

        provided = _extract_provided_token(request)
        if check_bearer_token(provided, self.expected_token):
            return await call_next(request)

        log.warning("Rejected unauthenticated request to %s", request.url.path)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
