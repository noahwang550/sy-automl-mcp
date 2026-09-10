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
from tools.auth import _store, current_bearer_token
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
    token = current_bearer_token.get("")
    if not token:
        return {
            "enabled": True,
            "upload_url": None,
            "instructions": (
                "Auth context missing. This tool must be called through the "
                "authenticated MCP endpoint."
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
    url = f"{MCP_UPLOAD_URL_BASE}/upload?token={token}"
    return {
        "enabled": True,
        "upload_url": url,
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


async def handle_post(scope, receive, send, token: str) -> None:
    req = Request(scope, receive)
    form = await req.form()
    file = form.get("file")
    if file is None or not getattr(file, "filename", None):
        resp = HTMLResponse(
            result_html("No file", "No file part in upload.", token),
            status_code=400,
        )
        await resp(scope, receive, send)
        return

    raw_id = (form.get("dataset_id") or "").strip()
    if raw_id:
        try:
            dataset_id = validate_id(raw_id, "dataset_id")
        except ValueError as exc:
            resp = HTMLResponse(
                result_html("Invalid dataset_id", str(exc), token),
                status_code=400,
            )
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
            result_html("Too large", f"File exceeds {MAX_UPLOAD_TOTAL_BYTES} bytes.", token),
            status_code=413,
        )
        await resp(scope, receive, send)
        return

    try:
        df = _read_df(out_file, fmt)
        _enforce_size_limits(df)
    except Exception as exc:
        out_file.unlink(missing_ok=True)
        resp = HTMLResponse(
            result_html("Parse failed", str(exc), token),
            status_code=400,
        )
        await resp(scope, receive, send)
        return

    extra = (
        f"<p>Rows: <strong>{len(df)}</strong> · Columns: <strong>{len(df.columns)}</strong></p>"
        f"<p>Tell your agent:</p>"
        f"<pre>The dataset is uploaded. Use dataset_id="
        f"<code>{_escape(dataset_id)}</code> with train_tabular / "
        f"predict_tabular / etc.</pre>"
    )
    resp = HTMLResponse(
        result_html("Uploaded ✓", f"dataset_id={dataset_id}", token, extra=extra)
    )
    await resp(scope, receive, send)
