"""Tests for the v0.6.5 session-based upload URL /u/{sid}.

Symmetric to the download-side session URL /d/{sid}. The session_id is
short (8-char url-safe, 48 bits) so it doesn't trigger agent-platform
redactors; the bearer auth was already proven at the
get_upload_instructions MCP tool-call time, so the session_id IS the
auth for the upload page.
"""
import asyncio

import config
from tools.upload import (
    _create_upload_session,
    _lookup_upload_session,
    _UPLOAD_SESSION_TTL,
    form_html_session,
    get_upload_instructions,
    handle_session_get,
)


def _make_scope(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "client": ["test", 0],
    }


def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def _collect_response(scope, sid=None):
    received = []

    async def send(msg):
        received.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    if sid is not None:
        asyncio.run(handle_session_get(scope, receive, send, sid))
    else:
        # Extract sid from path: /u/{sid}
        path = scope.get("path", "")
        sid_from_path = path[3:].split("/", 1)[0] if path.startswith("/u/") else ""
        asyncio.run(handle_session_get(scope, receive, send, sid_from_path))
    return received


def test_get_upload_instructions_returns_session_url(monkeypatch, isolated_artifacts):
    """get_upload_instructions must return /u/{sid}, not /upload?token=."""
    monkeypatch.setattr(config, "MCP_UPLOAD_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MCP_UPLOAD_URL_BASE", "http://host:9885", raising=False)
    monkeypatch.setattr("tools.upload.MCP_UPLOAD_ENABLED", True, raising=False)
    monkeypatch.setattr("tools.upload.MCP_UPLOAD_URL_BASE", "http://host:9885", raising=False)

    out = get_upload_instructions()
    assert out["success"] is True, out.get("error")
    data = out["data"]
    assert data["enabled"] is True
    url = data["upload_url"]
    assert url is not None, "upload_url must be set"
    # Must be the session-based format, NOT the legacy ?token= format.
    assert "/u/" in url, f"expected /u/ in url, got: {url}"
    assert "?token=" not in url, f"token query leaked into url: {url}"
    assert "token=" not in url, f"token leaked into url: {url}"
    sid = data["session_id"]
    assert sid and 6 <= len(sid) <= 16, f"sid length odd: {sid!r}"
    assert url.endswith(sid), f"url must end with sid: {url}"
    assert data["expires_in_seconds"] == _UPLOAD_SESSION_TTL
    # The session must be in the in-memory table.
    entry = _lookup_upload_session(sid)
    assert entry is not None, "session entry missing after get_upload_instructions"


def test_get_upload_instructions_when_disabled(monkeypatch, isolated_artifacts):
    """When upload disabled, response must say so and NOT issue a session."""
    monkeypatch.setattr("tools.upload.MCP_UPLOAD_ENABLED", False, raising=False)
    out = get_upload_instructions()
    assert out["success"] is True
    data = out["data"]
    assert data["enabled"] is False
    assert "upload_url" not in data or data["upload_url"] is None


def test_upload_session_get_serves_form(isolated_artifacts):
    """GET /u/{sid} serves the upload form HTML."""
    sid = _create_upload_session("test_agent")
    scope = _make_scope(f"/u/{sid}")
    received = _collect_response(scope)
    # http.response.start carries the status
    start = next(m for m in received if m["type"] == "http.response.start")
    assert start["status"] == 200
    body = b"".join(
        m.get("body", b"") for m in received if m["type"] == "http.response.body"
    )
    html = body.decode("utf-8", errors="replace")
    # Form must POST to /u/{sid} and not contain any token field.
    assert "<form" in html, "no form in HTML"
    assert f'action="/u/{sid}"' in html, f"form action wrong: {html[:200]}"
    assert 'name="token"' not in html, "session form must not carry hidden token"
    assert sid in html, "sid not in form"


def test_upload_session_get_rejects_invalid_sid(isolated_artifacts):
    """GET /u/{invalid-sid} must 410, not serve the form."""
    scope = _make_scope("/u/thisSidDoesNotExist")
    received = _collect_response(scope)
    start = next(m for m in received if m["type"] == "http.response.start")
    assert start["status"] == 410, f"expected 410 for invalid sid, got {start['status']}"


def test_upload_session_expires(monkeypatch, isolated_artifacts):
    """Expired sessions are rejected on lookup."""
    import time as _time

    sid = _create_upload_session("test_agent")
    # Warp the stored expiry into the past.
    from tools.upload import _UPLOAD_SESSIONS, _UPLOAD_SESSIONS_LOCK

    with _UPLOAD_SESSIONS_LOCK:
        _UPLOAD_SESSIONS[sid]["expires_at"] = int(_time.time()) - 1
    assert _lookup_upload_session(sid) is None, "expired session must not be valid"
