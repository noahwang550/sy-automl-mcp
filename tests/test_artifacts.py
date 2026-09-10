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
