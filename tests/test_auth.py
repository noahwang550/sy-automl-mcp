"""Tests for streamable-http Bearer-token authentication."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import config
from tools.auth import BearerTokenMiddleware, check_bearer_token, current_agent_id


# ---------------------------------------------------------------------------
# check_bearer_token unit tests
# ---------------------------------------------------------------------------
def test_check_bearer_token_auth_disabled() -> None:
    assert check_bearer_token(None, None) is True


def test_check_bearer_token_valid() -> None:
    assert check_bearer_token("Bearer correct", "correct") is True


def test_check_bearer_token_scheme_case_insensitive() -> None:
    assert check_bearer_token("bearer correct", "correct") is True
    assert check_bearer_token("BEARER correct", "correct") is True


def test_check_bearer_token_missing_header() -> None:
    assert check_bearer_token(None, "secret") is False


def test_check_bearer_token_wrong_token() -> None:
    assert check_bearer_token("Bearer wrong", "secret") is False


def test_check_bearer_token_wrong_scheme() -> None:
    assert check_bearer_token("Token secret", "secret") is False


def test_check_bearer_token_empty_token() -> None:
    assert check_bearer_token("Bearer", "secret") is False


def test_check_bearer_token_bare_token() -> None:
    assert check_bearer_token("secret", "secret") is True


def test_check_bearer_token_x_api_key_header() -> None:
    assert check_bearer_token("X-API-Key secret", "secret") is True


def test_check_bearer_token_empty_header_string() -> None:
    assert check_bearer_token("", "secret") is False
    assert check_bearer_token("   ", "secret") is False


# ---------------------------------------------------------------------------
# Middleware / ASGI integration tests
# ---------------------------------------------------------------------------
def _health_response(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _json_response(request: Request) -> JSONResponse:
    return JSONResponse({"data": "value"})


def _build_app(expected_token: str | None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", _health_response, methods=["GET"]),
            Route("/health", _health_response, methods=["GET"]),
            Route("/mcp", _json_response, methods=["POST"]),
        ]
    )
    app.add_middleware(BearerTokenMiddleware, expected_token=expected_token)
    return app


@pytest.fixture()
def auth_enabled(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "super-secret-token"
    monkeypatch.setattr(config, "MCP_API_TOKEN", token)
    return token


@pytest.fixture()
def auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MCP_API_TOKEN", None)


def test_middleware_rejects_missing_token(auth_enabled: str) -> None:
    app = _build_app(auth_enabled)
    client = TestClient(app)
    response = client.post("/mcp")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_middleware_rejects_wrong_token(auth_enabled: str) -> None:
    app = _build_app(auth_enabled)
    client = TestClient(app)
    response = client.post("/mcp", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_middleware_accepts_valid_bearer_token(auth_enabled: str) -> None:
    app = _build_app(auth_enabled)
    client = TestClient(app)
    response = client.post("/mcp", headers={"Authorization": f"Bearer {auth_enabled}"})
    assert response.status_code == 200
    assert response.json() == {"data": "value"}


def test_middleware_accepts_x_api_key_header(auth_enabled: str) -> None:
    app = _build_app(auth_enabled)
    client = TestClient(app)
    response = client.post("/mcp", headers={"X-API-Key": auth_enabled})
    assert response.status_code == 200


def test_middleware_rejects_wrong_x_api_key_header(auth_enabled: str) -> None:
    app = _build_app(auth_enabled)
    client = TestClient(app)
    response = client.post("/mcp", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_middleware_allows_health_when_auth_enabled(auth_enabled: str) -> None:
    app = _build_app(auth_enabled)
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_middleware_disabled_allows_all_requests(auth_disabled: None) -> None:
    app = _build_app(None)
    client = TestClient(app)
    response = client.post("/mcp")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Real FastMCP streamable-http app wiring check
# ---------------------------------------------------------------------------
def test_real_mcp_http_app_can_be_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the auth middleware can be registered on the actual MCP app."""
    monkeypatch.setattr(config, "MCP_API_TOKEN", "live-token")
    import server  # noqa: F401

    app = server.mcp.streamable_http_app()
    # Adding the middleware should not raise.
    app.add_middleware(BearerTokenMiddleware, expected_token="live-token")
    assert app is not None


# ---------------------------------------------------------------------------
# Multi-token + agent_id + audit (v0.5.0 extension)
# ---------------------------------------------------------------------------
def _agent_response(request: Request) -> JSONResponse:
    """Echo the agent_id resolved by the middleware via the ContextVar."""
    return JSONResponse({"agent_id": current_agent_id.get()})


def _build_multi_app(tokens_file: str | None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", _health_response, methods=["GET"]),
            Route("/health", _health_response, methods=["GET"]),
            Route("/mcp", _agent_response, methods=["POST"]),
        ]
    )
    app.add_middleware(BearerTokenMiddleware, tokens_file=tokens_file)
    return app


def _write_tokens(path: Path, tokens: list[dict]) -> None:
    path.write_text(
        json.dumps({"version": 1, "tokens": tokens}),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _reset_token_store() -> None:
    """Reset the module-level TokenStore singleton between tests."""
    from tools.auth import _store

    _store.configure(tokens_file=None, legacy_token=None)
    yield
    _store.configure(tokens_file=None, legacy_token=None)


def test_multi_token_missing_returns_401(tmp_path: Path) -> None:
    _write_tokens(
        tmp_path / "tokens.json",
        [{"token": "t1", "agent_id": "a1", "enabled": True}],
    )
    client = TestClient(_build_multi_app(str(tmp_path / "tokens.json")))
    response = client.post("/mcp")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_multi_token_wrong_returns_401(tmp_path: Path) -> None:
    _write_tokens(
        tmp_path / "tokens.json",
        [{"token": "t1", "agent_id": "a1", "enabled": True}],
    )
    client = TestClient(_build_multi_app(str(tmp_path / "tokens.json")))
    response = client.post("/mcp", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_multi_token_agent_a_resolved(tmp_path: Path) -> None:
    _write_tokens(
        tmp_path / "tokens.json",
        [{"token": "t1", "agent_id": "agent-a", "enabled": True}],
    )
    client = TestClient(_build_multi_app(str(tmp_path / "tokens.json")))
    response = client.post("/mcp", headers={"Authorization": "Bearer t1"})
    assert response.status_code == 200
    assert response.json() == {"agent_id": "agent-a"}


def test_multi_token_isolates_agents(tmp_path: Path) -> None:
    _write_tokens(
        tmp_path / "tokens.json",
        [
            {"token": "t1", "agent_id": "agent-a", "enabled": True},
            {"token": "t2", "agent_id": "agent-b", "enabled": True},
        ],
    )
    client = TestClient(_build_multi_app(str(tmp_path / "tokens.json")))
    r1 = client.post("/mcp", headers={"Authorization": "Bearer t1"})
    r2 = client.post("/mcp", headers={"Authorization": "Bearer t2"})
    assert r1.json() == {"agent_id": "agent-a"}
    assert r2.json() == {"agent_id": "agent-b"}


def test_multi_token_disabled_token_rejected(tmp_path: Path) -> None:
    _write_tokens(
        tmp_path / "tokens.json",
        [
            {"token": "t1", "agent_id": "a1", "enabled": True},
            {"token": "t2", "agent_id": "a2", "enabled": False},
        ],
    )
    client = TestClient(_build_multi_app(str(tmp_path / "tokens.json")))
    response = client.post("/mcp", headers={"Authorization": "Bearer t2"})
    assert response.status_code == 401


def test_multi_token_health_exempt(tmp_path: Path) -> None:
    _write_tokens(
        tmp_path / "tokens.json",
        [{"token": "t1", "agent_id": "a1", "enabled": True}],
    )
    client = TestClient(_build_multi_app(str(tmp_path / "tokens.json")))
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_multi_token_hot_reload(tmp_path: Path) -> None:
    """Rewriting the tokens file and forcing a reload swaps active tokens."""
    from tools.auth import _store

    path = tmp_path / "tokens.json"
    _write_tokens(path, [{"token": "t1", "agent_id": "a1", "enabled": True}])
    client = TestClient(_build_multi_app(str(path)))

    assert client.post("/mcp", headers={"Authorization": "Bearer t1"}).status_code == 200
    _write_tokens(path, [{"token": "t2", "agent_id": "a2", "enabled": True}])
    # Direct reload bypasses the 5s mtime gate (simulates wait > 5s).
    _store._load()
    assert client.post("/mcp", headers={"Authorization": "Bearer t1"}).status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer t2"}).status_code == 200


def test_legacy_fallback_assigns_default_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tokens_file; only legacy MCP_API_TOKEN → agent_id == 'default'."""
    monkeypatch.setattr(config, "MCP_API_TOKEN", "legacy-secret")
    app = Starlette(routes=[Route("/mcp", _agent_response, methods=["POST"])])
    app.add_middleware(BearerTokenMiddleware, expected_token="legacy-secret")
    client = TestClient(app)
    response = client.post("/mcp", headers={"Authorization": "Bearer legacy-secret"})
    assert response.status_code == 200
    assert response.json() == {"agent_id": "default"}


def test_corrupt_tokens_file_keeps_last_good(tmp_path: Path) -> None:
    """A malformed tokens file must not wipe the last good config."""
    from tools.auth import _store

    path = tmp_path / "tokens.json"
    _write_tokens(path, [{"token": "t1", "agent_id": "a1", "enabled": True}])
    client = TestClient(_build_multi_app(str(path)))
    # First request loads the good config.
    assert client.post("/mcp", headers={"Authorization": "Bearer t1"}).status_code == 200
    # Corrupt the file; direct reload should keep the last good config.
    path.write_text("{ not valid json", encoding="utf-8")
    _store._load()
    assert client.post("/mcp", headers={"Authorization": "Bearer t1"}).status_code == 200


def test_audit_log_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(config, "MCP_AUDIT_LOG_DIR", str(audit_dir))
    _write_tokens(
        tmp_path / "tokens.json",
        [{"token": "t1", "agent_id": "agent-a", "enabled": True}],
    )
    client = TestClient(_build_multi_app(str(tmp_path / "tokens.json")))
    client.post("/mcp", headers={"Authorization": "Bearer t1"})
    # The auth module reads MCP_AUDIT_LOG_DIR at import time; patch the module attr.
    import tools.auth as auth_mod

    monkeypatch.setattr(auth_mod, "MCP_AUDIT_LOG_DIR", str(audit_dir))
    # Trigger another request so the patched path takes effect.
    client.post("/mcp", headers={"Authorization": "Bearer t1"})

    files = list(audit_dir.glob("audit-*.jsonl"))
    assert files, "audit jsonl file should exist"
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["agent_id"] == "agent-a"
    assert record["method"] == "POST"
    assert record["path"] == "/mcp"
    assert record["status"] == 200
    assert "duration_ms" in record
