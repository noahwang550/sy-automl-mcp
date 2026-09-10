"""Multi-token Bearer auth + agent_id injection + audit logging.

Drop-in replacement for the original single-token ``BearerTokenMiddleware``:

- Inherits ``BaseHTTPMiddleware`` (same protocol as before — ``server.py``
  mounting unchanged in shape).
- Multi-token: ``MCP_API_TOKENS_FILE`` points to a JSON token table
  (hot-reloaded, 5s mtime check).
- Backward compatible: falls back to legacy single ``MCP_API_TOKEN`` env var.
- Audit: one JSON line per request (``agent_id``, ``method``, ``path``,
  ``status``, ``duration``). Daily-rotated files under
  ``MCP_AUDIT_LOG_DIR``, plus stdout.
- ``current_agent_id`` ContextVar lets downstream tools read the caller's
  ``agent_id`` (e.g. to partition artifacts by agent).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config import MCP_AUDIT_LOG_DIR

log = logging.getLogger("sy-automl-mcp")

# Downstream tools read this to scope artifacts by agent.
current_agent_id: ContextVar[str] = ContextVar("current_agent_id", default="anonymous")
# The raw bearer token the caller presented (used by get_upload_instructions
# to embed ?token= in the browser upload URL — the LLM can't see the token
# otherwise since it lives in the client config, not LLM context).
current_bearer_token: ContextVar[str] = ContextVar("current_bearer_token", default="")

_RELOAD_INTERVAL = 5.0  # seconds between stat() checks


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """Loads and hot-reloads the token table."""

    def __init__(self) -> None:
        self._tokens_file: str | None = None
        self._legacy_token: str | None = None
        self._checked_at: float = 0.0
        self._mtime: float = -1.0
        self._by_hash: dict[str, str] = {}  # sha256(token) -> agent_id

    def configure(
        self,
        tokens_file: str | None,
        legacy_token: str | None,
    ) -> None:
        """Reconfigure the store. Forces a reload on next ``maybe_reload()``."""
        self._tokens_file = (tokens_file or "").strip() or None
        self._legacy_token = legacy_token or None
        self._mtime = -1.0
        self._checked_at = 0.0
        self._by_hash = {}

    @property
    def is_configured(self) -> bool:
        """True if any auth source (tokens file or legacy token) is set."""
        return bool(self._tokens_file) or bool(self._legacy_token)

    def _load(self) -> None:
        entries: list[dict[str, Any]] = []
        if self._tokens_file:
            try:
                data = json.loads(Path(self._tokens_file).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    entries = data.get("tokens", []) or []
                elif isinstance(data, list):
                    entries = data
                else:
                    entries = []
            except OSError:
                log.error(
                    "tokens file unreadable: %r (keeping last good config)",
                    self._tokens_file,
                )
                return
            except json.JSONDecodeError:
                log.exception("tokens file parse error (keeping last good config)")
                return
        elif self._legacy_token:
            entries = [{"token": self._legacy_token, "agent_id": "default", "enabled": True}]

        self._by_hash = {
            _hash(e["token"]): str(e.get("agent_id") or "unknown")
            for e in entries
            if e.get("enabled", True) and e.get("token")
        }
        if not self._by_hash:
            log.warning("token store EMPTY: all authenticated endpoints will return 401")
        else:
            log.info("token store loaded: %d active token(s)", len(self._by_hash))

    def maybe_reload(self) -> None:
        """Stat the tokens file at most once per ``_RELOAD_INTERVAL``; reload on mtime change."""
        now = time.monotonic()
        if now - self._checked_at < _RELOAD_INTERVAL:
            return
        self._checked_at = now
        if not self._tokens_file:
            # Legacy single-token mode: no file to stat; load once if not yet loaded.
            if self._mtime < 0 and self._legacy_token:
                self._load()
                self._mtime = 0.0
            return
        try:
            mtime = Path(self._tokens_file).stat().st_mtime
        except OSError:
            log.error(
                "tokens file stat failed: %r (keeping last good config)",
                self._tokens_file,
            )
            return
        if mtime != self._mtime:
            self._load()
            self._mtime = mtime

    def lookup(self, token: str) -> str | None:
        """Return the ``agent_id`` for *token*, or ``None`` if unknown."""
        if not token:
            return None
        digest = _hash(token)
        for known, agent_id in self._by_hash.items():
            if hmac.compare_digest(known, digest):
                return agent_id
        return None


# Module-level singleton. ``BearerTokenMiddleware`` reads from this so that
# ``server.py``'s ``BearerTokenMiddleware(app, tokens_file=..., legacy_token=...)``
# can configure the shared store without exposing it as a global import.
_store = TokenStore()


def check_bearer_token(auth_header: str | None, expected: str | None) -> bool:
    """Backward-compatible function (existing tests depend on it).

    New behavior: ignored on the multi-token path — the multi-token path is
    handled in the middleware. Kept so ``tests/test_auth.py`` imports do not
    break at collection time.
    """
    if expected is None:
        return True
    if not isinstance(auth_header, str):
        return False
    auth_header = auth_header.strip()
    if not auth_header:
        return False
    parts = auth_header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in {"bearer", "x-api-key"}:
        candidate = parts[1]
    else:
        candidate = auth_header
    return hmac.compare_digest(candidate, expected)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Starlette middleware. Same class name + base class as the original.

    Constructor accepts both new (``tokens_file``) and legacy
    (``expected_token`` / ``legacy_token``) kwargs so ``server.py`` can mount
    it regardless of which env is set.
    """

    def __init__(
        self,
        app,
        tokens_file: str | None = None,
        legacy_token: str | None = None,
        expected_token: str | None = None,  # legacy kwarg alias for legacy_token
        exempt_paths: set[str] | None = None,
        exempt_paths_all_methods: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        # `expected_token` is the legacy kwarg name; alias it to `legacy_token`.
        if expected_token and not legacy_token:
            legacy_token = expected_token
        self._auth_enabled = bool(tokens_file or legacy_token)
        _store.configure(tokens_file=tokens_file, legacy_token=legacy_token)
        self.exempt_paths: set[str] = exempt_paths or {"/", "/health"}
        # Paths that skip auth regardless of method (the handler does its own
        # token check). Used by /upload so browsers can pass ?token= in URL.
        self.exempt_paths_all_methods: set[str] = exempt_paths_all_methods or set()
        # Force initial load at startup (don't wait for first request).
        _store.maybe_reload()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Auth disabled (no tokens_file, no legacy token): pass through.
        # Note: `_McpOrHealthApp` in server.py handles GET /health BEFORE this
        # middleware runs; the exempt_paths check below is a defensive fallback.
        if not self._auth_enabled:
            return await call_next(request)

        if request.url.path in self.exempt_paths_all_methods:
            return await call_next(request)

        if request.method == "GET" and request.url.path in self.exempt_paths:
            return await call_next(request)

        _store.maybe_reload()
        provided = request.headers.get("Authorization") or request.headers.get("X-API-Key")
        token = self._extract_bearer(provided)
        agent_id = _store.lookup(token)

        started = time.monotonic()
        if agent_id is None:
            log.warning("Rejected unauthenticated request to %s", request.url.path)
            self._audit(request, "anonymous", 401, started)
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        request.state.agent_id = agent_id
        ctx = current_agent_id.set(agent_id)
        tok_ctx = current_bearer_token.set(token)
        status = 0
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception:
            status = 500
            raise
        finally:
            current_agent_id.reset(ctx)
            current_bearer_token.reset(tok_ctx)
            self._audit(request, agent_id, status, started)

    @staticmethod
    def _extract_bearer(header: str | None) -> str:
        if not header:
            return ""
        header = header.strip()
        if not header:
            return ""
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "x-api-key"}:
            return parts[1].strip()
        return header

    @staticmethod
    def _audit(
        request: Request,
        agent_id: str,
        status: int,
        started: float,
    ) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "agent_id": agent_id,
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "client": request.client.host if request.client else "-",
        }
        line = json.dumps(record, ensure_ascii=False)
        log.info("audit %s", line)  # stdout -> docker logs
        audit_dir = MCP_AUDIT_LOG_DIR
        if audit_dir:
            try:
                audit_path = Path(audit_dir)
                audit_path.mkdir(parents=True, exist_ok=True)
                day = datetime.now(UTC).strftime("%Y%m%d")
                with (audit_path / f"audit-{day}.jsonl").open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                log.exception("failed to write audit file")
