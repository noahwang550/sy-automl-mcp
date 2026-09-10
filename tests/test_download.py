"""HTTP-level tests for /download ASGI handler — full/range/401/403/404/413."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import config
from config import configure


@pytest.fixture
def isolated_artifacts(tmp_path, monkeypatch):
    configure(tmp_path)
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MCP_DOWNLOAD_SIGNING_KEY", "test_key_down")
    monkeypatch.setattr(artifacts, "MCP_ARTIFACT_BASE_URL", "http://test:9885")
    monkeypatch.setattr(artifacts, "MCP_DOWNLOAD_ENABLED", True)
    monkeypatch.setattr(artifacts, "MCP_AUDIT_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(artifacts, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    yield tmp_path


def _make_scope(path: str, method="GET", headers=None, query=""):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode("utf-8"),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 12345),
    }


def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutinefunction(coro) else asyncio.run(coro)


def _collect_response(scope, receive, send_mock):
    """Drive the handler synchronously and return collected status/headers/body."""
    asyncio.get_event_loop().run_until_complete(_drive(scope, receive, send_mock))
    return send_mock.captured


async def _drive(scope, receive, send_mock):
    await _handler_call(scope, receive, send_mock)


class _SendMock:
    def __init__(self):
        self.captured = {"status": None, "headers": {}, "body": b""}
        self._more = True

    async def __call__(self, msg):
        if msg["type"] == "http.response.start":
            self.captured["status"] = msg["status"]
            self.captured["headers"] = {k.decode(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            self.captured["body"] += msg.get("body", b"")


def _call_handler(scope):
    from tools.artifacts import download_handler
    send = _SendMock()
    asyncio.run(_drive_with_handler(download_handler, scope, send))
    return send.captured


async def _drive_with_handler(handler, scope, send):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    await handler(scope, receive, send)


def _make_file(rel: str, content: bytes):
    p = config.ARTIFACTS_DIR.resolve() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _token(path: str):
    from tools.artifacts import build_presigned_token
    return build_presigned_token(path, int(time.time()) + 3600, "test_agent")


def test_download_full_200(isolated_artifacts):
    _make_file("models/m01/leaderboard.csv", b"a,b,c\n1,2,3\n4,5,6\n")
    tok = _token("models/m01/leaderboard.csv")
    scope = _make_scope("/download", query=f"token={tok}&path=models/m01/leaderboard.csv")
    r = _call_handler(scope)
    assert r["status"] == 200
    assert r["headers"]["content-type"] == "text/csv; charset=utf-8"
    assert r["headers"]["accept-ranges"] == "bytes"
    assert "attachment" in r["headers"]["content-disposition"]
    assert r["body"].startswith(b"a,b,c")


def test_download_range_206(isolated_artifacts):
    _make_file("models/m01/big.bin", b"0123456789" * 20)  # 200 bytes
    tok = _token("models/m01/big.bin")
    scope = _make_scope("/download", headers={"range": "bytes=0-9"},
                        query=f"token={tok}&path=models/m01/big.bin")
    r = _call_handler(scope)
    assert r["status"] == 206
    assert r["headers"]["content-range"] == "bytes 0-9/200"
    assert r["headers"]["content-length"] == "10"
    assert len(r["body"]) == 10
    assert r["body"] == b"0123456789"


def test_download_range_suffix(isolated_artifacts):
    _make_file("models/m01/big.bin", b"0123456789" * 20)
    tok = _token("models/m01/big.bin")
    scope = _make_scope("/download", headers={"range": "bytes=-10"},
                        query=f"token={tok}&path=models/m01/big.bin")
    r = _call_handler(scope)
    assert r["status"] == 206
    assert len(r["body"]) == 10


def test_download_416_range_out_of_bounds(isolated_artifacts):
    _make_file("models/m01/big.bin", b"0123456789" * 20)
    tok = _token("models/m01/big.bin")
    scope = _make_scope("/download", headers={"range": "bytes=500-600"},
                        query=f"token={tok}&path=models/m01/big.bin")
    r = _call_handler(scope)
    assert r["status"] == 416
    assert r["headers"]["content-range"] == "bytes */200"


def test_download_401_missing_token(isolated_artifacts):
    scope = _make_scope("/download", query="path=models/m01/x.csv")
    r = _call_handler(scope)
    assert r["status"] == 401


def test_download_401_bad_token(isolated_artifacts):
    _make_file("models/m01/x.csv", b"abc")
    scope = _make_scope("/download", query="token=bogus&path=models/m01/x.csv")
    r = _call_handler(scope)
    assert r["status"] == 401


def test_download_403_traversal(isolated_artifacts):
    tok = _token("../../etc/passwd")
    scope = _make_scope("/download", query=f"token={tok}&path=../../etc/passwd")
    r = _call_handler(scope)
    assert r["status"] == 403


def test_download_404_missing_file(isolated_artifacts):
    tok = _token("models/m01/missing.csv")
    scope = _make_scope("/download", query=f"token={tok}&path=models/m01/missing.csv")
    r = _call_handler(scope)
    assert r["status"] == 404


def test_download_413_oversize(isolated_artifacts, monkeypatch):
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MAX_DOWNLOAD_BYTES", 10)
    _make_file("models/m01/big.bin", b"x" * 200)
    tok = _token("models/m01/big.bin")
    scope = _make_scope("/download", query=f"token={tok}&path=models/m01/big.bin")
    r = _call_handler(scope)
    assert r["status"] == 413


def test_download_audit_line(isolated_artifacts):
    _make_file("models/m01/a.csv", b"abc")
    tok = _token("models/m01/a.csv")
    scope = _make_scope("/download", query=f"token={tok}&path=models/m01/a.csv")
    _call_handler(scope)
    audit_dir = Path(isolated_artifacts / "audit")
    files = list(audit_dir.glob("audit-*.jsonl"))
    assert files
    line = files[0].read_text().strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["event"] == "download"
    assert entry["path"] == "models/m01/a.csv"
    assert entry["status"] == 200
    assert entry["agent_id"] == "test_agent"


def test_download_disabled_503(isolated_artifacts, monkeypatch):
    from tools import artifacts
    monkeypatch.setattr(artifacts, "MCP_DOWNLOAD_ENABLED", False)
    _make_file("models/m01/a.csv", b"abc")
    tok = _token("models/m01/a.csv")
    scope = _make_scope("/download", query=f"token={tok}&path=models/m01/a.csv")
    r = _call_handler(scope)
    assert r["status"] == 503
