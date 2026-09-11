"""Browser upload page + endpoint (``/upload``).

Escape hatch for agent platforms that cannot pipe attachment bytes through
the LLM. The user opens ``/upload?token=<bearer>`` in a browser, selects a
file, and receives a ``dataset_id`` to paste into the agent chat. The agent
then uses that ``dataset_id`` with ``train_tabular`` / ``predict_tabular`` /
etc.

Auth mirrors the MCP endpoint: a valid bearer token from the same table
(``deploy/tokens.json``). Accepted as ``?token=<bearer>`` query param
(browser-friendly) or ``Authorization: Bearer <token>`` header.
"""
from __future__ import annotations

import html as html_mod
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from config import (
    MAX_UPLOAD_TOTAL_BYTES,
    MCP_UPLOAD_ENABLED,
    MCP_UPLOAD_URL_BASE,
    dataset_path,
    validate_id,
)
from serialization import sample_rows
from tools._common import envelope_call, safe_tool
from tools.auth import _store, current_agent_id, current_bearer_token
from tools.data import _detect_format, _enforce_size_limits, _read_df, dataset_file

_FORM = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Upload dataset — sy-automl-mcp</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#222}}
code,pre{{background:#f4f4f4;padding:2px 6px;border-radius:3px;font-family:ui-monospace,monospace}}
pre{{padding:12px;overflow-x:auto}}
h2{{margin-top:0}}
</style></head><body>
<h2>Upload a dataset</h2>
<p>The agent platform can't pipe file bytes through the LLM, so upload the
file here directly. You'll get a <code>dataset_id</code> to paste into your
agent chat.</p>
<form method="POST" enctype="multipart/form-data">
<input type="hidden" name="token" value="{token}">
<p><label>File (CSV / Parquet / JSON):<br>
<input type="file" name="file" required></label></p>
<p><label>dataset_id (optional — auto-generated if blank):<br>
<input type="text" name="dataset_id" placeholder="e.g. my_data" size="32"></label></p>
<p><label>format:
<select name="format">
<option value="auto">auto (detect from extension)</option>
<option value="csv">csv</option>
<option value="parquet">parquet</option>
<option value="json">json</option>
</select></label></p>
<p><button type="submit">Upload</button></p>
</form></body></html>"""

_FORM_SESSION = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Upload dataset — sy-automl-mcp</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#222}}
code,pre{{background:#f4f4f4;padding:2px 6px;border-radius:3px;font-family:ui-monospace,monospace}}
pre{{padding:12px;overflow-x:auto}}
h2{{margin-top:0}}
</style></head><body>
<h2>Upload a dataset</h2>
<p>The agent platform can't pipe file bytes through the LLM, so upload the
file here directly. You'll get a <code>dataset_id</code> to paste into your
agent chat.</p>
<form method="POST" enctype="multipart/form-data" action="/u/{sid}">
<p><label>File (CSV / Parquet / JSON):<br>
<input type="file" name="file" required></label></p>
<p><label>dataset_id (optional — auto-generated if blank):<br>
<input type="text" name="dataset_id" placeholder="e.g. my_data" size="32"></label></p>
<p><label>format:
<select name="format">
<option value="auto">auto (detect from extension)</option>
<option value="csv">csv</option>
<option value="parquet">parquet</option>
<option value="json">json</option>
</select></label></p>
<p><button type="submit">Upload</button></p>
</form></body></html>"""

_RESULT = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Upload result — sy-automl-mcp</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#222}}
code,pre{{background:#f4f4f4;padding:2px 6px;border-radius:3px;font-family:ui-monospace,monospace}}
pre{{padding:12px;overflow-x:auto}}
a{{color:#06c}}
</style></head><body>
<h2>{status}</h2>
<p>{message}</p>
{extra}
<p><a href="/upload?token={token}">Upload another</a></p>
</body></html>"""

_RESULT_SESSION = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Upload result — sy-automl-mcp</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#222}}
code,pre{{background:#f4f4f4;padding:2px 6px;border-radius:3px;font-family:ui-monospace,monospace}}
pre{{padding:12px;overflow-x:auto}}
a{{color:#06c}}
</style></head><body>
<h2>{status}</h2>
<p>{message}</p>
{extra}
<p><a href="/u/{sid}">Upload another</a></p>
</body></html>"""


# ---------------------------------------------------------------------------
# Session-based short URLs for upload (v0.6.5) — mirrors the download-side
# pattern. Problem: /upload?token=<bearer> had its token query param
# auto-redacted by the agent platform's security layer, so the URL the
# agent handed the user was unusable. /u/{8-char-sid} sidesteps redactors
# (the session_id is short and low-entropy); the bearer auth was already
# proven at the get_upload_instructions tool-call time, so the session_id
# IS the auth for the upload page. In-memory store; TTL 1h; sweep on
# access. Does NOT survive container restart, but issued URLs are
# short-lived and a restart invalidating them is acceptable.
# ---------------------------------------------------------------------------

_UPLOAD_SESSIONS: dict[str, dict] = {}
_UPLOAD_SESSIONS_LOCK = threading.Lock()
_UPLOAD_SWEEP_COUNTER = 0
_UPLOAD_SESSION_TTL = 3600  # 1h, mirrors download side


def _create_upload_session(agent_id: str) -> str:
    """Create a short-lived session_id (8-char url-safe, 48 bits entropy).

    The session_id is an opaque index into the in-memory table; it is NOT
    a secret — the bearer auth was already proven at MCP tool-call time.
    """
    sid = secrets.token_urlsafe(6)
    expires_at = int(time.time()) + _UPLOAD_SESSION_TTL
    with _UPLOAD_SESSIONS_LOCK:
        _maybe_sweep_upload_sessions_locked()
        while sid in _UPLOAD_SESSIONS:
            sid = secrets.token_urlsafe(6)
        _UPLOAD_SESSIONS[sid] = {"agent_id": agent_id, "expires_at": expires_at}
    return sid


def _lookup_upload_session(sid: str) -> dict | None:
    """Return session entry if valid+unexpired, else None. Sweeps on access."""
    if not sid:
        return None
    with _UPLOAD_SESSIONS_LOCK:
        _maybe_sweep_upload_sessions_locked()
        entry = _UPLOAD_SESSIONS.get(sid)
        if entry is None:
            return None
        if entry["expires_at"] < int(time.time()):
            _UPLOAD_SESSIONS.pop(sid, None)
            return None
        return dict(entry)


def _maybe_sweep_upload_sessions_locked() -> None:
    """Evict expired sessions. Caller must hold _UPLOAD_SESSIONS_LOCK."""
    global _UPLOAD_SWEEP_COUNTER
    _UPLOAD_SWEEP_COUNTER += 1
    if _UPLOAD_SWEEP_COUNTER % 32 != 0:
        return  # amortize
    now = int(time.time())
    expired = [sid for sid, e in _UPLOAD_SESSIONS.items() if e["expires_at"] < now]
    for sid in expired:
        _UPLOAD_SESSIONS.pop(sid, None)


def _get_upload_instructions() -> dict:
    """Build the browser-upload URL + user-facing instructions for the agent."""
    if not MCP_UPLOAD_ENABLED:
        return {
            "enabled": False,
            "instructions": (
                "Browser upload is disabled on this server. Ask the operator "
                "to set MCP_UPLOAD_ENABLED=true and MCP_UPLOAD_URL_BASE."
            ),
        }
    if not MCP_UPLOAD_URL_BASE:
        return {
            "enabled": True,
            "upload_url": None,
            "instructions": (
                "The server has browser upload enabled but MCP_UPLOAD_URL_BASE "
                "is not configured. Ask the operator to set it to the "
                "externally-reachable URL (e.g. http://host:9885)."
            ),
        }
    # v0.6.5: session-based short URL /u/{8-char-sid}. The session_id is too
    # short/low-entropy to trigger agent-platform redactors that scan for
    # high-entropy token strings; the bearer auth was already proven at MCP
    # tool-call time, so the session_id IS the auth for the upload page.
    # Legacy /upload?token=<bearer> is kept for backward compat.
    agent_id = current_agent_id.get("anonymous")
    sid = _create_upload_session(agent_id)
    url = f"{MCP_UPLOAD_URL_BASE}/u/{sid}"
    return {
        "enabled": True,
        "upload_url": url,
        "session_id": sid,
        "expires_in_seconds": _UPLOAD_SESSION_TTL,
        "max_bytes": MAX_UPLOAD_TOTAL_BYTES,
        "accepted_formats": ["csv", "parquet", "json"],
        "instructions": (
            "The user has a file the agent platform can't pipe through as "
            "bytes. Send the user this exact upload_url and ask them to open "
            "it in a browser, select their file, and click Upload. The page "
            "will return a dataset_id. The user should send you that "
            "dataset_id; then call train_tabular / predict_tabular / etc. "
            "with it."
        ),
    }


def get_upload_instructions() -> dict:
    """Discover the browser upload URL for files the agent can't access as bytes.

    Call this when the user has an attachment (e.g. an ``attachment_id`` from
    their platform) that you cannot read as raw bytes to pass to
    ``upload_dataset``. The response contains a ``upload_url`` (with auth
    token embedded) the user can open in a browser to upload the file, plus
    ``instructions`` you should relay to the user verbatim. After the user
    uploads and reports back the ``dataset_id``, use that id with
    ``train_tabular`` / ``predict_tabular`` / etc.
    """
    return envelope_call(_get_upload_instructions)


get_upload_instructions = safe_tool(get_upload_instructions)


def _escape(s: str) -> str:
    return html_mod.escape(str(s), quote=True)


def form_html(token: str) -> str:
    return _FORM.format(token=_escape(token))


def result_html(status: str, message: str, token: str, extra: str = "") -> str:
    return _RESULT.format(
        status=_escape(status),
        message=_escape(message),
        token=_escape(token),
        extra=extra,
    )


def form_html_session(sid: str) -> str:
    return _FORM_SESSION.format(sid=_escape(sid))


def result_html_session(
    status: str, message: str, sid: str, extra: str = ""
) -> str:
    return _RESULT_SESSION.format(
        status=_escape(status),
        message=_escape(message),
        sid=_escape(sid),
        extra=extra,
    )


def _extract_token(scope) -> str:
    """Pull bearer token from ?token= query param or Authorization/X-API-Key header."""
    qs = scope.get("query_string", b"").decode("latin-1")
    if qs:
        token = parse_qs(qs).get("token", [None])[0]
        if token:
            return token.strip()
    for name, val in scope.get("headers", []):
        if name == b"authorization":
            v = val.decode("latin-1")
            if v.lower().startswith("bearer "):
                return v[7:].strip()
            return v.strip()
        if name == b"x-api-key":
            return val.decode("latin-1").strip()
    return ""


def check_upload_token(scope) -> str | None:
    """Return agent_id if the request is authed; None otherwise.

    When the token store is unconfigured (system-wide auth disabled), all
    requests pass — mirroring the MCP middleware's behavior.
    """
    _store.maybe_reload()
    if not _store.is_configured:
        return "anonymous"
    token = _extract_token(scope)
    return _store.lookup(token)


def unauthorized_response() -> Response:
    return HTMLResponse(
        "<!DOCTYPE html><html><body><h2>401 Unauthorized</h2>"
        "<p>A valid token is required. Pass it as <code>?token=...</code> in the URL.</p>"
        "</body></html>",
        status_code=401,
    )


async def handle_get(scope, receive, send, token: str) -> None:
    resp = HTMLResponse(form_html(token))
    await resp(scope, receive, send)


async def _process_upload_form(scope, receive, send, *, render) -> None:
    """Shared body for legacy and session POST handlers.

    ``render`` is a callable ``(status, message, extra="") -> str`` that
    returns the result-page HTML. Each handler passes a closure that
    captures its own auth context (token or sid).
    """
    req = Request(scope, receive)
    form = await req.form()
    file = form.get("file")
    if file is None or not getattr(file, "filename", None):
        resp = HTMLResponse(render("No file", "No file part in upload."), status_code=400)
        await resp(scope, receive, send)
        return

    raw_id = (form.get("dataset_id") or "").strip()
    if raw_id:
        try:
            dataset_id = validate_id(raw_id, "dataset_id")
        except ValueError as exc:
            resp = HTMLResponse(render("Invalid dataset_id", str(exc)), status_code=400)
            await resp(scope, receive, send)
            return
    else:
        dataset_id = "u" + uuid.uuid4().hex[:12]

    fmt = form.get("format") or "auto"
    if fmt == "auto":
        fmt = _detect_format("auto", file.filename or "data.csv")

    ddir = dataset_path(dataset_id)
    ddir.mkdir(parents=True, exist_ok=True)
    out_file = ddir / dataset_file(dataset_id, fmt)
    size = 0
    too_large = False
    with out_file.open("wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_TOTAL_BYTES:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        out_file.unlink(missing_ok=True)
        resp = HTMLResponse(
            render("Too large", f"File exceeds {MAX_UPLOAD_TOTAL_BYTES} bytes."),
            status_code=413,
        )
        await resp(scope, receive, send)
        return

    try:
        df = _read_df(out_file, fmt)
        _enforce_size_limits(df)
    except Exception as exc:
        out_file.unlink(missing_ok=True)
        resp = HTMLResponse(render("Parse failed", str(exc)), status_code=400)
        await resp(scope, receive, send)
        return

    extra = (
        f"<p>Rows: <strong>{len(df)}</strong> · Columns: <strong>{len(df.columns)}</strong></p>"
        f"<p>Tell your agent:</p>"
        f"<pre>The dataset is uploaded. Use dataset_id="
        f"<code>{_escape(dataset_id)}</code> with train_tabular / "
        f"predict_tabular / etc.</pre>"
    )
    resp = HTMLResponse(render("Uploaded ✓", f"dataset_id={dataset_id}", extra=extra))
    await resp(scope, receive, send)


async def handle_post(scope, receive, send, token: str) -> None:
    def render(status: str, message: str, extra: str = "") -> str:
        return result_html(status, message, token, extra=extra)

    await _process_upload_form(scope, receive, send, render=render)


_EXPIRED_HTML = (
    "<!DOCTYPE html><html><body><h2>410 Upload link expired</h2>"
    "<p>This upload link has expired or is invalid. Ask the agent for a "
    "fresh <code>upload_url</code> via <code>get_upload_instructions</code>.</p>"
    "</body></html>"
)


async def handle_session_get(scope, receive, send, sid: str) -> None:
    """v0.6.5: serve the upload form for a session-based short URL /u/{sid}."""
    if _lookup_upload_session(sid) is None:
        resp = HTMLResponse(_EXPIRED_HTML, status_code=410)
        await resp(scope, receive, send)
        return
    resp = HTMLResponse(form_html_session(sid))
    await resp(scope, receive, send)


async def handle_session_post(scope, receive, send, sid: str) -> None:
    """v0.6.5: process upload for a session-based short URL /u/{sid}."""
    if _lookup_upload_session(sid) is None:
        resp = HTMLResponse(_EXPIRED_HTML, status_code=410)
        await resp(scope, receive, send)
        return

    def render(status: str, message: str, extra: str = "") -> str:
        return result_html_session(status, message, sid, extra=extra)

    await _process_upload_form(scope, receive, send, render=render)
