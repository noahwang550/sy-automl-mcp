"""Tests for streamable-http Bearer-token authentication."""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import config
from tools.auth import BearerTokenMiddleware, check_bearer_token


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
