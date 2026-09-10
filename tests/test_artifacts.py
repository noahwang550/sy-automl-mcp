"""Unit tests for tools/artifacts.py — presigned tokens + path resolution."""
from __future__ import annotations

import time
from pathlib import Path

import config
import pytest
from config import configure


@pytest.fixture
def isolated_artifacts(tmp_path, monkeypatch):
    configure(tmp_path)
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MCP_DOWNLOAD_SIGNING_KEY", "test_key_123")
    monkeypatch.setattr(artifacts, "MCP_ARTIFACT_BASE_URL", "http://test:9885")
    monkeypatch.setattr(artifacts, "MCP_DOWNLOAD_ENABLED", True)
    monkeypatch.setattr(artifacts, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    monkeypatch.setattr(artifacts, "MCP_AUDIT_LOG_DIR", str(tmp_path / "audit"))
    yield tmp_path


def test_presigned_roundtrip(isolated_artifacts):
    from tools.artifacts import build_presigned_token, verify_presigned_token
    exp = int(time.time()) + 3600
    tok = build_presigned_token("models/m01/leaderboard.csv", exp, "agent1")
    assert tok is not None
    ok, agent = verify_presigned_token(tok, "models/m01/leaderboard.csv")
    assert ok is True
    assert agent == "agent1"


def test_presigned_tamper_path(isolated_artifacts):
    from tools.artifacts import build_presigned_token, verify_presigned_token
    exp = int(time.time()) + 3600
    tok = build_presigned_token("models/m01/a.csv", exp, "agent1")
    ok, _ = verify_presigned_token(tok, "models/m01/b.csv")
    assert ok is False


def test_presigned_expired(isolated_artifacts):
    from tools.artifacts import build_presigned_token, verify_presigned_token
    exp = int(time.time()) - 10
    tok = build_presigned_token("models/m01/a.csv", exp, "agent1")
    ok, _ = verify_presigned_token(tok, "models/m01/a.csv")
    assert ok is False


def test_resolve_rejects_traversal(isolated_artifacts):
    from tools.artifacts import resolve_artifact_path
    with pytest.raises(ValueError):
        resolve_artifact_path("../../etc/passwd")
    with pytest.raises(ValueError):
        resolve_artifact_path("models/../..")
    with pytest.raises(ValueError):
        resolve_artifact_path("/etc/passwd")
    with pytest.raises(ValueError):
        resolve_artifact_path("models\\m01")


def test_resolve_rejects_symlink_escape(tmp_path, isolated_artifacts, monkeypatch):
    """Symlinks that resolve outside artifacts/ must be rejected."""
    from tools.artifacts import resolve_artifact_path
    # Create a symlink inside artifacts/ pointing to /etc/passwd
    base = config.ARTIFACTS_DIR.resolve()
    base.mkdir(parents=True, exist_ok=True)
    link = base / "escape.csv"
    try:
        link.symlink_to("/etc/passwd")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this fs")
    with pytest.raises(ValueError):
        resolve_artifact_path("escape.csv")


def test_resolve_accepts_valid_path(isolated_artifacts):
    from tools.artifacts import resolve_artifact_path
    base = config.ARTIFACTS_DIR.resolve()
    (base / "models" / "m01").mkdir(parents=True, exist_ok=True)
    (base / "models" / "m01" / "report.json").write_text("{}")
    p = resolve_artifact_path("models/m01/report.json")
    assert p.exists()
    assert p.name == "report.json"


def test_list_artifacts_basic(isolated_artifacts):
    from tools.artifacts import _list_artifacts
    base = config.ARTIFACTS_DIR.resolve()
    (base / "models" / "m01").mkdir(parents=True, exist_ok=True)
    (base / "models" / "m01" / "a.csv").write_text("x")
    (base / "models" / "m01" / "b.json").write_text("{}")
    out = _list_artifacts("m01", None, 0, 100)
    assert out["count"] == 2
    assert out["next_skip"] is None
    paths = [e["path"] for e in out["entries"]]
    assert "models/m01/a.csv" in paths
    assert "models/m01/b.json" in paths


def test_list_artifacts_pagination(isolated_artifacts):
    from tools.artifacts import _list_artifacts
    base = config.ARTIFACTS_DIR.resolve()
    (base / "models" / "m01").mkdir(parents=True, exist_ok=True)
    for i in range(15):
        (base / "models" / "m01" / f"f{i:02d}.csv").write_text("x")
    out = _list_artifacts("m01", None, 0, 10)
    assert out["count"] == 10
    assert out["next_skip"] == 10
    out2 = _list_artifacts("m01", None, 10, 10)
    assert out2["count"] == 5
    assert out2["next_skip"] is None


def test_get_artifact_url_requires_base(isolated_artifacts, monkeypatch):
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MCP_ARTIFACT_BASE_URL", "")
    out = artifacts._get_artifact_url("models/m01/a.csv", 3600)
    assert out["success"] is False


def test_get_artifact_url_path_format(isolated_artifacts):
    """v0.6.4: URL must use short session_id, no high-entropy token."""
    from tools.artifacts import _get_artifact_url
    base = config.ARTIFACTS_DIR.resolve()
    (base / "models" / "m01").mkdir(parents=True, exist_ok=True)
    (base / "models" / "m01" / "leaderboard.csv").write_text("x")
    out = _get_artifact_url("models/m01/leaderboard.csv", 3600)
    assert out["success"] is True
    url = out["data"]["url"]
    # No token in query string
    assert "?token=" not in url, f"token must not be in query: {url}"
    # No v1.{exp}.{agent}.{sig} path-token format either
    assert "/download/v1." not in url, f"legacy path-token format leaked: {url}"
    # New format: /d/{session_id}/{rel_path}
    assert "/d/" in url, f"must use session-based format: {url}"
    # session_id is short (8 chars from secrets.token_urlsafe(6))
    after = url.split("/d/", 1)[1]
    sid = after.split("/", 1)[0]
    assert 6 <= len(sid) <= 16, f"session_id length odd: {sid!r}"
    assert out["data"]["format"] == "session_v1"
    assert out["data"]["session_id"] == sid
    # Path preserved at the end
    assert url.endswith("/models/m01/leaderboard.csv")


def test_download_session_roundtrip(isolated_artifacts):
    """Session URL actually serves the file bytes via /d/{sid}/{path}."""
    import asyncio
    from tools.artifacts import _get_artifact_url, download_handler
    base = config.ARTIFACTS_DIR.resolve()
    (base / "models" / "m01").mkdir(parents=True, exist_ok=True)
    (base / "models" / "m01" / "x.csv").write_bytes(b"hello,world\n")
    out = _get_artifact_url("models/m01/x.csv", 3600)
    url = out["data"]["url"]
    # Strip the base URL to get just the path
    path_part = url.split(":9885", 1)[-1] if ":9885" in url else url

    # Simulate an ASGI GET request to /d/{sid}/models/m01/x.csv
    received = []
    async def send(msg):
        if msg["type"] == "http.response.body":
            received.append(msg.get("body", b""))
    scope = {
        "type": "http",
        "method": "GET",
        "path": url.split("9885", 1)[-1] if "9885" in url else "/d/x/y",
        "query_string": b"",
        "headers": [],
        "client": ["test", 0],
    }
    asyncio.run(download_handler(scope, lambda: None, send))
    body = b"".join(received)
    assert b"hello,world" in body, f"body didn't contain file: {body[:100]}"


def test_download_session_path_mismatch(isolated_artifacts):
    """Session bound to path A must not work for path B."""
    import asyncio
    from tools.artifacts import _get_artifact_url, download_handler, _lookup_download_session
    base = config.ARTIFACTS_DIR.resolve()
    (base / "models" / "m01").mkdir(parents=True, exist_ok=True)
    (base / "models" / "m01" / "a.csv").write_text("a")
    (base / "models" / "m01" / "b.csv").write_text("b")
    out = _get_artifact_url("models/m01/a.csv", 3600)
    sid = out["data"]["session_id"]
    # Try to use the session_id for a different path
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/d/{sid}/models/m01/b.csv",
        "query_string": b"",
        "headers": [],
        "client": ["test", 0],
    }
    received = []
    async def send(msg):
        if msg["type"] == "http.response.start":
            received.append(msg["status"])
    asyncio.run(download_handler(scope, lambda: None, send))
    assert 403 in received, f"path mismatch should 403, got: {received}"


# ---------------------------------------------------------------------------
# get_artifact_bytes — inline-bytes bridge
# ---------------------------------------------------------------------------


@pytest.fixture
def inline_artifacts(isolated_artifacts, monkeypatch):
    """Same as isolated_artifacts but also patches MAX_INLINE_BYTES on the
    artifacts module (the import-time copy in tools.artifacts, not just config)."""
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MAX_INLINE_BYTES", 1024)
    # MAX_CHUNK_BYTES must be <= MAX_INLINE_BYTES so the clamp logic sees a
    # reduced ceiling in tests too (in prod, config.py computes this at import
    # via min(); the test fixture patches it explicitly).
    monkeypatch.setattr(artifacts, "MAX_CHUNK_BYTES", 1024)
    yield


def _make_file(rel: str, content: bytes) -> Path:
    from config import ARTIFACTS_DIR
    p = ARTIFACTS_DIR.resolve() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_get_artifact_bytes_roundtrip(inline_artifacts):
    import base64
    from tools.artifacts import _get_artifact_bytes
    _make_file("models/m01/leaderboard.csv", b"col_a,col_b\n1,2\n")
    out = _get_artifact_bytes("models/m01/leaderboard.csv")
    assert out["success"] is True
    data = out["data"]
    assert data["path"] == "models/m01/leaderboard.csv"
    assert data["size_bytes"] == 16
    assert data["content_type"] == "text/csv; charset=utf-8"
    assert data["encoding"] == "base64"
    assert base64.b64decode(data["content_base64"]) == b"col_a,col_b\n1,2\n"


def test_get_artifact_bytes_rejects_traversal(inline_artifacts):
    from tools.artifacts import _get_artifact_bytes
    with pytest.raises(ValueError):
        _get_artifact_bytes("../../etc/passwd")


def test_get_artifact_bytes_rejects_missing(inline_artifacts):
    from tools.artifacts import _get_artifact_bytes
    with pytest.raises(FileNotFoundError):
        _get_artifact_bytes("models/m01/does_not_exist.csv")


def test_get_artifact_bytes_rejects_directory(inline_artifacts, monkeypatch):
    from tools.artifacts import _get_artifact_bytes
    _make_file("models/m01/inside.csv", b"x")
    with pytest.raises(ValueError):
        _get_artifact_bytes("models/m01")


def test_get_artifact_bytes_rejects_oversize(inline_artifacts):
    from tools.artifacts import _get_artifact_bytes
    _make_file("models/m01/big.bin", b"x" * 2048)  # MAX_INLINE_BYTES is 1024
    out = _get_artifact_bytes("models/m01/big.bin")
    assert out["success"] is False
    assert "MAX_INLINE_BYTES" in str(out.get("error", ""))


def test_get_artifact_bytes_disabled_when_bridge_off(inline_artifacts, monkeypatch):
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MCP_DOWNLOAD_ENABLED", False)
    _make_file("models/m01/a.csv", b"x")
    out = artifacts._get_artifact_bytes("models/m01/a.csv")
    assert out["success"] is False
    assert "disabled" in str(out.get("error", "")).lower()


def test_get_artifact_bytes_strips_artifacts_prefix(inline_artifacts):
    import base64
    from tools.artifacts import _get_artifact_bytes
    _make_file("models/m01/a.csv", b"hello")
    out = _get_artifact_bytes("artifacts/models/m01/a.csv")
    assert out["success"] is True
    assert out["data"]["path"] == "models/m01/a.csv"
    assert base64.b64decode(out["data"]["content_base64"]) == b"hello"


def test_get_artifact_bytes_binary_integrity(inline_artifacts):
    import base64
    from tools.artifacts import _get_artifact_bytes
    payload = bytes(range(256)) * 4  # 1024 bytes exactly
    _make_file("models/m01/b.bin", payload)
    out = _get_artifact_bytes("models/m01/b.bin")
    assert out["success"] is True
    assert out["data"]["size_bytes"] == 1024
    assert base64.b64decode(out["data"]["content_base64"]) == payload


# ---------------------------------------------------------------------------
# get_artifact_chunk — chunked-pull bridge (v0.6.2)
# ---------------------------------------------------------------------------


def test_get_artifact_chunk_roundtrip(inline_artifacts):
    import base64
    from tools.artifacts import _get_artifact_chunk
    _make_file("models/m01/predictions.csv", b"abcdefghij")  # 10 bytes
    out = _get_artifact_chunk("models/m01/predictions.csv", offset=0, length=4)
    assert out["success"] is True
    d = out["data"]
    assert d["offset"] == 0
    assert d["length"] == 4
    assert d["total_size"] == 10
    assert d["final"] is False
    assert base64.b64decode(d["content_base64"]) == b"abcd"


def test_get_artifact_chunk_final_chunk(inline_artifacts):
    import base64
    from tools.artifacts import _get_artifact_chunk
    _make_file("models/m01/x.csv", b"abcdefghij")  # 10 bytes
    out = _get_artifact_chunk("models/m01/x.csv", offset=6, length=4)
    d = out["data"]
    assert d["length"] == 4
    assert d["final"] is True
    assert base64.b64decode(d["content_base64"]) == b"ghij"


def test_get_artifact_chunk_reassembly(inline_artifacts):
    import base64
    from tools.artifacts import _get_artifact_chunk
    payload = bytes(range(256)) * 4  # 1024 bytes
    _make_file("models/m01/big.bin", payload)
    collected = bytearray()
    offset = 0
    final = False
    while not final:
        out = _get_artifact_chunk("models/m01/big.bin", offset=offset, length=256)
        d = out["data"]
        collected += base64.b64decode(d["content_base64"])
        offset = d["offset"] + d["length"]
        final = d["final"]
    assert bytes(collected) == payload
    assert offset == 1024


def test_get_artifact_chunk_offset_past_eof(inline_artifacts):
    from tools.artifacts import _get_artifact_chunk
    _make_file("models/m01/small.csv", b"ab")  # 2 bytes
    out = _get_artifact_chunk("models/m01/small.csv", offset=10, length=4)
    d = out["data"]
    assert d["length"] == 0
    assert d["content_base64"] == ""
    assert d["total_size"] == 2
    assert d["final"] is True


def test_get_artifact_chunk_clamps_oversize_length(inline_artifacts):
    import base64
    from tools.artifacts import _get_artifact_chunk
    payload = b"x" * 4096  # MAX_CHUNK_BYTES is 65536 but MAX_INLINE_BYTES=1024
    _make_file("models/m01/mid.bin", payload)
    out = _get_artifact_chunk("models/m01/mid.bin", offset=0, length=99999)
    d = out["data"]
    # length must be clamped to MAX_CHUNK_BYTES (which is min(64KB, MAX_INLINE_BYTES=1024))
    assert d["length"] <= 1024
    assert d["length"] == 1024
    assert d["final"] is False
    assert base64.b64decode(d["content_base64"]) == b"x" * 1024


def test_get_artifact_chunk_rejects_directory(inline_artifacts):
    from tools.artifacts import _get_artifact_chunk
    _make_file("models/m01/inside.csv", b"x")
    with pytest.raises(ValueError):
        _get_artifact_chunk("models/m01", offset=0, length=10)


def test_get_artifact_chunk_rejects_traversal(inline_artifacts):
    from tools.artifacts import _get_artifact_chunk
    with pytest.raises(ValueError):
        _get_artifact_chunk("../../etc/passwd", offset=0, length=10)


def test_get_artifact_chunk_rejects_missing(inline_artifacts):
    from tools.artifacts import _get_artifact_chunk
    with pytest.raises(FileNotFoundError):
        _get_artifact_chunk("models/m01/missing.csv", offset=0, length=10)


def test_get_artifact_chunk_rejects_bad_offset(inline_artifacts):
    from tools.artifacts import _get_artifact_chunk
    _make_file("models/m01/a.csv", b"abc")
    with pytest.raises(ValueError):
        _get_artifact_chunk("models/m01/a.csv", offset=-1, length=10)
    with pytest.raises(ValueError):
        _get_artifact_chunk("models/m01/a.csv", offset=0, length=0)


def test_get_artifact_chunk_disabled_when_bridge_off(inline_artifacts, monkeypatch):
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MCP_DOWNLOAD_ENABLED", False)
    _make_file("models/m01/a.csv", b"abc")
    out = artifacts._get_artifact_chunk("models/m01/a.csv", offset=0, length=10)
    assert out["success"] is False
    assert "disabled" in str(out.get("error", "")).lower()
