"""Artifact download bridge: presigned URLs + /download ASGI handler.

Bridges the gap that agent platforms could push bytes into the MCP container
(via upload_dataset_chunk / finalize_dataset / /upload) but had no way to pull
bytes out. Exposes:

- ``list_artifacts(model_id, subpath, skip, limit)`` — MCP tool that walks
  the artifacts tree (path-validated, traversal-rejecting).
- ``get_artifact_url(path, ttl_seconds)`` — MCP tool that issues a
  presigned HMAC-SHA256 URL bound to (path, expires_at, agent_id).
- ``download_handler(scope, receive, send)`` — ASGI app that serves
  ``GET /download?token=<presigned>&path=<rel>`` with Range support.

Path safety: every path is normalized then resolved and checked to remain
inside ``ARTIFACTS_DIR``. Symbolic links that escape are caught by the
``is_relative_to`` check on the resolved path.

Auth model: ``/download`` is exempt from the bearer-token middleware
(see ``server.py``); the presigned token IS the auth. Signing key resolution
is fail-closed: if no key is configured, download_handler returns 503 and
``get_artifact_url`` returns a failure envelope.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, parse_qs

from config import (
    LOGS_DIR,
    MAX_DOWNLOAD_BYTES,
    MCP_ARTIFACT_BASE_URL,
    MCP_AUDIT_LOG_DIR,
    MCP_DOWNLOAD_ENABLED,
    MCP_DOWNLOAD_SIGNING_KEY,
    MCP_DOWNLOAD_URL_TTL_SECONDS,
    directory_size_bytes,
    model_path,
    validate_id,
)
import config as _config
from serialization import failure, success, to_jsonable
from tasks.manager import Task  # noqa: F401  (re-exported for symmetry)

from ._common import envelope_call, safe_tool

PRESIGN_VERSION = "v1"
_CHUNK = 64 * 1024
_LIST_CAP_DEFAULT = 5000
_AUDIT_LOCK = None  # set lazily; module is imported before threading in some paths

CONTENT_TYPES = {
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".log": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".pkl": "application/octet-stream",
    ".pt": "application/octet-stream",
    ".pth": "application/octet-stream",
    ".bin": "application/octet-stream",
    ".model": "application/octet-stream",
    ".parquet": "application/octet-stream",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".zip": "application/zip",
}


# ---------------------------------------------------------------------------
# Signing key — fail-closed
# ---------------------------------------------------------------------------


def _signing_key() -> bytes | None:
    """Resolve the HMAC signing key, or None if unconfigured (fail-closed)."""
    if MCP_DOWNLOAD_SIGNING_KEY:
        return MCP_DOWNLOAD_SIGNING_KEY.encode("utf-8")
    return None


def _b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode("utf-8")


def build_presigned_token(path: str, expires_at: int, agent_id: str) -> str | None:
    """Issue a presigned token for ``path`` valid until ``expires_at`` (unix).

    Returns None if no signing key is configured (caller surfaces failure).
    The token binds path + expiry + agent_id into the HMAC so tampering with
    any of them invalidates the signature.
    """
    key = _signing_key()
    if key is None:
        return None
    msg = f"download|{path}|{expires_at}|{agent_id}".encode("utf-8")
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return f"{PRESIGN_VERSION}.{expires_at}.{_b64e(agent_id)}.{sig}"


def verify_presigned_token(token: str, path: str) -> tuple[bool, str]:
    """Verify ``token`` against ``path``. Returns (ok, agent_id_or_reason)."""
    key = _signing_key()
    if key is None:
        return False, "no_signing_key"
    parts = token.split(".")
    if len(parts) != 4:
        return False, "malformed"
    ver, exp_s, ag_b64, sig = parts
    if ver != PRESIGN_VERSION:
        return False, "bad_version"
    try:
        expires_at = int(exp_s)
    except ValueError:
        return False, "bad_expiry"
    if expires_at < int(time.time()):
        return False, "expired"
    try:
        agent_id = _b64d(ag_b64)
    except Exception:
        return False, "bad_agent"
    msg = f"download|{path}|{expires_at}|{agent_id}".encode("utf-8")
    expected = hmac.new(key, msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False, "bad_sig"
    return True, agent_id


# ---------------------------------------------------------------------------
# Path resolution — rejects traversal, absolute, symlink escape
# ---------------------------------------------------------------------------


def resolve_artifact_path(path: str) -> Path:
    """Resolve a user-supplied relative path to a safe Path inside ARTIFACTS_DIR.

    Rejects absolute paths, ``..`` segments, backslashes, and any path that
    resolves outside ARTIFACTS_DIR (including via symbolic links).
    """
    if not path or not isinstance(path, str):
        raise ValueError("empty path")
    raw = path.strip()
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("absolute path rejected")
    p = raw.lstrip("/")
    if p.startswith("artifacts/"):
        p = p[len("artifacts/"):]
    if not p:
        raise ValueError("empty path")
    if "\\" in p:
        raise ValueError("backslash in path")
    parts = p.split("/")
    for seg in parts:
        # Allow subdirs like models/m01/leaderboard.csv — validate_id rejects
        # path separators and ".." by its regex; also rejects empty segments.
        validate_id(seg, "path_segment")
    base = _config.ARTIFACTS_DIR.resolve()
    resolved = (base / Path(*parts)).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError("path escapes artifacts root")
    return resolved


def _content_type(name: str) -> str:
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit_log_path() -> Path | None:
    base = MCP_AUDIT_LOG_DIR
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
        return Path(base) / f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    return None


def _audit_download(scope: dict, agent_id: str, path: str, nbytes: int,
                    status: int, started: float, range_hdr: str | None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "download",
        "agent_id": agent_id or "anonymous",
        "path": path,
        "bytes": nbytes,
        "status": status,
        "ip": (scope.get("client") or ["?"])[0],
        "range": range_hdr,
        "duration_ms": int((time.time() - started) * 1000),
    }
    line = json.dumps(entry, ensure_ascii=False)
    # Best-effort; never let audit failure break the response.
    try:
        p = _audit_log_path()
        if p is not None:
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
    # Also echo to stdout for container logs (single line, parseable).
    print(f"audit: {line}", flush=True)


# ---------------------------------------------------------------------------
# MCP tools: list_artifacts, get_artifact_url
# ---------------------------------------------------------------------------


def _walk_tree(root: Path, cap: int) -> tuple[list[dict], int]:
    entries: list[dict] = []
    total_bytes = 0
    base = _config.ARTIFACTS_DIR.resolve()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(base).as_posix()
            entries.append({
                "path": rel,
                "type": "file",
                "size_bytes": st.st_size,
                "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
            total_bytes += st.st_size
            if len(entries) >= cap:
                return entries, total_bytes
    return entries, total_bytes


def _list_artifacts(model_id: str | None, subpath: str | None,
                    skip: int, limit: int) -> dict[str, Any]:
    if skip < 0:
        raise ValueError("skip must be >= 0")
    if limit <= 0 or limit > 10000:
        raise ValueError("limit must be in [1, 10000]")
    base = _config.ARTIFACTS_DIR.resolve()
    if model_id is not None:
        validate_id(model_id, "model_id")
        root = base / "models" / model_id
    else:
        root = base
    if subpath:
        for seg in subpath.split("/"):
            if seg:
                validate_id(seg, "subpath_segment")
        root = root / subpath
    root = root.resolve()
    if not root.is_relative_to(base):
        raise ValueError("subpath escapes artifacts root")
    if not root.exists():
        raise FileNotFoundError(f"path not found: {root.relative_to(base)}")
    entries, total = _walk_tree(root, skip + limit)
    truncated = len(entries) == skip + limit
    page = entries[skip:skip + limit]
    next_skip = skip + limit if (len(page) == limit and truncated) else None
    return {
        "root": str(root.relative_to(base)),
        "entries": page,
        "total_bytes_in_full_tree": total,
        "count": len(page),
        "skip": skip,
        "limit": limit,
        "next_skip": next_skip,
    }


def list_artifacts(
    model_id: str | None = None,
    subpath: str | None = None,
    skip: int = 0,
    limit: int = _LIST_CAP_DEFAULT,
) -> dict[str, Any]:
    """List files in the artifacts tree with size + mtime.

    Paginated via ``skip``/``limit``; ``next_skip`` in the response is set
    when more entries remain. Pass ``model_id`` to scope to one model dir;
    combine with ``subpath`` to drill further.
    """
    return envelope_call(_list_artifacts, model_id, subpath, skip, limit)


def _get_artifact_url(path: str, ttl_seconds: int) -> dict[str, Any]:
    if not MCP_DOWNLOAD_ENABLED:
        return failure("download bridge disabled (MCP_DOWNLOAD_ENABLED=false)")
    base = MCP_ARTIFACT_BASE_URL
    if not base:
        return failure("MCP_ARTIFACT_BASE_URL is not configured; ask the operator to set it")
    if not (60 <= ttl_seconds <= 86400):
        raise ValueError("ttl_seconds must be in [60, 86400]")
    # Resolve first — rejects bad paths AND verifies file existence (virtual
    # archives excepted).
    virtual_archive = path.endswith(".tar.gz") and not _path_exists(path)
    if not virtual_archive:
        resolved = resolve_artifact_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"artifact not found: {path}")
    agent_id = _current_agent_id() or "anonymous"
    expires_at = int(time.time()) + ttl_seconds
    token = build_presigned_token(path, expires_at, agent_id)
    if token is None:
        return failure("MCP_DOWNLOAD_SIGNING_KEY is not configured; cannot sign URLs")
    rel = path.lstrip("/")
    if rel.startswith("artifacts/"):
        rel = rel[len("artifacts/"):]
    url = f"{base}/download?token={token}&path={quote(rel, safe='')}"
    expires_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    return success({"url": url, "expires_at": expires_iso, "path": rel})


def get_artifact_url(path: str, ttl_seconds: int = MCP_DOWNLOAD_URL_TTL_SECONDS) -> dict[str, Any]:
    """Issue a presigned download URL for an artifact.

    URL is bound to (path, expiry, agent_id) via HMAC-SHA256 and expires
    after ``ttl_seconds`` (default 1h, max 24h). The bearer-token middleware
    exempts ``/download``; the presigned token is the auth.
    """
    return envelope_call(_get_artifact_url, path, ttl_seconds)


def _path_exists(path: str) -> bool:
    try:
        return resolve_artifact_path(path).exists()
    except ValueError:
        return False


def _current_agent_id() -> str | None:
    """Best-effort read of the current request's agent_id from auth module."""
    try:
        from tools.auth import current_agent_id
        v = current_agent_id.get()
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# /download ASGI handler
# ---------------------------------------------------------------------------


async def download_handler(scope: dict, receive, send) -> None:
    """ASGI handler for ``GET /download?token=...&path=...``."""
    started = time.time()
    if not MCP_DOWNLOAD_ENABLED:
        await _send_json(send, 503, {"detail": "download disabled"})
        _audit_download(scope, "?", "?", 0, 503, started, None)
        return
    key = _signing_key()
    if key is None:
        await _send_json(send, 503, {"detail": "download signing key not configured"})
        _audit_download(scope, "?", "?", 0, 503, started, None)
        return

    qs = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="replace"))
    token = (qs.get("token") or [""])[0]
    path_arg = (qs.get("path") or [""])[0]
    range_hdr = _read_request_header(scope, "range")

    if not token or not path_arg:
        await _send_json(send, 401, {"detail": "missing token or path"})
        _audit_download(scope, "?", path_arg or "?", 0, 401, started, range_hdr)
        return
    ok, agent_or_reason = verify_presigned_token(token, path_arg)
    if not ok:
        await _send_json(send, 401, {"detail": "unauthorized"})
        _audit_download(scope, agent_or_reason, path_arg, 0, 401, started, range_hdr)
        return
    agent_id = agent_or_reason

    try:
        resolved = resolve_artifact_path(path_arg)
    except ValueError as e:
        await _send_json(send, 403, {"detail": "forbidden"})
        _audit_download(scope, agent_id, path_arg, 0, 403, started, range_hdr)
        return

    # HEAD: same headers, no body. GET: stream bytes. Range optional.
    method = scope.get("method", "GET").upper()

    if not resolved.exists():
        await _send_json(send, 404, {"detail": "not found"})
        _audit_download(scope, agent_id, path_arg, 0, 404, started, range_hdr)
        return

    if resolved.is_dir():
        # Serve directory as tar.gz stream (virtual archive).
        await _serve_dir_as_tar(send, resolved, path_arg, range_hdr, scope,
                                agent_id, started)
        return

    await _serve_file(send, resolved, path_arg, method, range_hdr, scope,
                      agent_id, started)


def _read_request_header(scope: dict, name: str) -> str | None:
    for k, v in scope.get("headers", []):
        if k.decode("latin-1").lower() == name:
            return v.decode("latin-1")
    return None


async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [[b"content-type", b"application/json"], [b"content-length", str(len(payload)).encode()]],
    })
    await send({"type": "http.response.body", "body": payload, "more_body": False})


def _parse_range(range_hdr: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range request. Returns (start, end_inclusive) or None.

    Returns (0, size-1) for full-file responses when range_hdr is None.
    Raises ValueError on malformed or unsatisfiable ranges.
    """
    if not range_hdr:
        return 0, size - 1
    if not range_hdr.startswith("bytes="):
        raise ValueError("only bytes= range unit supported")
    spec = range_hdr[len("bytes="):]
    if "," in spec:
        raise ValueError("multi-range not supported")
    if "-" not in spec:
        raise ValueError("malformed range")
    s, e = spec.split("-", 1)
    if s == "" and e == "":
        raise ValueError("malformed range")
    if s == "":
        # suffix: last N bytes
        n = int(e)
        if n <= 0:
            raise ValueError("malformed range")
        if n > size:
            n = size
        return max(0, size - n), size - 1
    start = int(s)
    end = size - 1 if e == "" else int(e)
    if start > end or start >= size:
        raise ValueError("unsatisfiable")
    if end >= size:
        end = size - 1
    return start, end


async def _serve_file(send, path: Path, path_arg: str, method: str,
                      range_hdr: str | None, scope: dict, agent_id: str,
                      started: float) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        await _send_json(send, 404, {"detail": "not found"})
        _audit_download(scope, agent_id, path_arg, 0, 404, started, range_hdr)
        return

    try:
        rng = _parse_range(range_hdr, size)
    except ValueError as e:
        await _send_simple(send, 416, {
            "content-range": f"bytes */{size}",
            "content-type": "text/plain; charset=utf-8",
            "content-length": "0",
        }, b"")
        _audit_download(scope, agent_id, path_arg, 0, 416, started, range_hdr)
        return

    start, end = rng or (0, size - 1)
    is_partial = range_hdr is not None
    status = 206 if is_partial else 200
    length = end - start + 1

    if not is_partial and length > MAX_DOWNLOAD_BYTES:
        await _send_json(send, 413, {"detail": "file too large; use Range header"})
        _audit_download(scope, agent_id, path_arg, 0, 413, started, range_hdr)
        return
    if is_partial and length > MAX_DOWNLOAD_BYTES:
        await _send_json(send, 413, {"detail": "range too large; reduce segment size"})
        _audit_download(scope, agent_id, path_arg, 0, 413, started, range_hdr)
        return

    ctype = _content_type(path.name)
    fname = path.name
    headers = [
        [b"content-type", ctype.encode("latin-1")],
        [b"content-length", str(length).encode("latin-1")],
        [b"accept-ranges", b"bytes"],
        [b"content-disposition", f'attachment; filename="{fname}"'.encode("latin-1")],
    ]
    if is_partial:
        headers.append([b"content-range", f"bytes {start}-{end}/{size}".encode("latin-1")])
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    if method == "HEAD":
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        _audit_download(scope, agent_id, path_arg, 0, status, started, range_hdr)
        return

    bytes_sent = 0
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            if not chunk:
                break
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
            bytes_sent += len(chunk)
            remaining -= len(chunk)
    await send({"type": "http.response.body", "body": b"", "more_body": False})
    _audit_download(scope, agent_id, path_arg, bytes_sent, status, started, range_hdr)


async def _send_simple(send, status: int, headers: dict, body: bytes) -> None:
    h = []
    for k, v in headers.items():
        h.append([k.encode("latin-1"), v.encode("latin-1")])
    await send({"type": "http.response.start", "status": status, "headers": h})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _serve_dir_as_tar(send, dir_path: Path, path_arg: str,
                            range_hdr: str | None, scope: dict,
                            agent_id: str, started: float) -> None:
    """Stream a directory as a tar.gz archive."""
    import io
    import tarfile
    # Range on a virtual archive is rejected — size is unknown until compression.
    if range_hdr is not None:
        await _send_json(send, 416, {"detail": "range not supported on archive"})
        _audit_download(scope, agent_id, path_arg, 0, 416, started, range_hdr)
        return
    # Pre-estimate uncompressed size; reject if too large (conservative).
    uncompressed = directory_size_bytes(dir_path)
    if uncompressed > MAX_DOWNLOAD_BYTES * 4:
        await _send_json(send, 413, {"detail": "archive too large"})
        _audit_download(scope, agent_id, path_arg, 0, 413, started, range_hdr)
        return

    fname = f"{dir_path.name}.tar.gz"
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"application/gzip"],
            [b"content-disposition", f'attachment; filename="{fname}"'.encode("latin-1")],
            [b"transfer-encoding", b"chunked"],
        ],
    })
    if scope.get("method", "GET").upper() == "HEAD":
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        _audit_download(scope, agent_id, path_arg, 0, 200, started, range_hdr)
        return

    bytes_sent = 0
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(dir_path.rglob("*")):
            if p.is_file():
                arcname = p.relative_to(dir_path.parent).as_posix()
                tar.add(p, arcname=arcname, recursive=False)
                # Flush whatever tar has written so far.
                data = buf.getvalue()
                if data:
                    await _send_chunk(send, data)
                    bytes_sent += len(data)
                    buf.seek(0)
                    buf.truncate(0)
        tar.close()
    tail = buf.getvalue()
    if tail:
        await _send_chunk(send, tail)
        bytes_sent += len(tail)
    await send({"type": "http.response.body", "body": b"", "more_body": False})
    _audit_download(scope, agent_id, path_arg, bytes_sent, 200, started, range_hdr)


async def _send_chunk(send, data: bytes) -> None:
    # HTTP chunked transfer encoding framing.
    frame = f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n"
    await send({"type": "http.response.body", "body": frame, "more_body": True})


# Public exports (safe_tool wrapped for MCP registration).
list_artifacts = safe_tool(list_artifacts)
get_artifact_url = safe_tool(get_artifact_url)
