"""Live streamable-http Bearer-token auth end-to-end test for sy-automl-mcp.

Runs inside the Docker container, starts ``server.py`` in the background,
then exercises raw HTTP and the real MCP streamable-http client with and
without authentication.

Environment consumed:
    - ``MCP_TRANSPORT`` (set to ``http`` by the runner)
    - ``MCP_PORT`` / ``MCP_HOST`` (default 8000 / 127.0.0.1)
    - ``MCP_API_TOKEN`` (set by the runner for the auth-enabled cases)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("http_auth_e2e")

BASE_URL = f"http://{os.environ.get('MCP_HOST', '127.0.0.1')}:{os.environ.get('MCP_PORT', '8000')}"
TOKEN = os.environ.get("MCP_API_TOKEN", "secret123")
TIMEOUT_S = 4.0


def _http(method: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Make a raw HTTP request and return (status, body)."""
    req = urllib.request.Request(  # noqa: S310 — internal test harness only
        BASE_URL + path,
        method=method,
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get_status(path: str, headers: dict[str, str] | None = None) -> int:
    status, _ = _http("GET", path, headers)
    return status


def get_body(path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    return _http("GET", path, headers)


@asynccontextmanager
async def mcp_client_session(token: str | None = None):
    """Yield an initialized MCP ``ClientSession`` over streamable-http."""
    client_headers: dict[str, str] = {}
    if token:
        client_headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=client_headers,
        timeout=httpx.Timeout(30.0, read=300.0),
    ) as http_client:
        async with streamable_http_client(
            f"{BASE_URL}/mcp",
            http_client=http_client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def test_mcp_client_auth() -> None:
    """Case 8: full MCP protocol with and without valid token."""
    log.info("Case 8a: authenticated MCP protocol (24 tools)")
    async with mcp_client_session(TOKEN) as session:
        result = await session.list_tools()
        tool_names = [t.name for t in result.tools]
        log.info("authenticated tools: %d", len(tool_names))
        assert len(tool_names) == 24, f"expected 24 tools, got {len(tool_names)}"

    log.info("Case 8b: unauthenticated MCP protocol must fail")
    try:
        async with mcp_client_session(None) as session:
            await session.list_tools()
    except Exception as exc:  # noqa: BLE001 — we expect an failure
        log.info("unauthenticated MCP client failed as expected: %s", exc)
    else:
        raise AssertionError("expected unauthenticated MCP client to fail")  # noqa: TRY301


def test_raw_http() -> None:
    """Raw HTTP status cases 1-7 and 11."""
    # Case 1: no auth header -> 401
    status, body = get_body("/mcp")
    log.info("Case 1 GET /mcp no auth -> %s %s", status, body)
    assert status == 401, f"expected 401, got {status}"
    assert json.loads(body) == {"detail": "Unauthorized"}

    # Case 2: wrong token -> 401
    status, body = get_body("/mcp", {"Authorization": "Bearer wrong"})
    log.info("Case 2 GET /mcp wrong token -> %s %s", status, body)
    assert status == 401, f"expected 401, got {status}"
    assert json.loads(body) == {"detail": "Unauthorized"}

    # Case 3: valid Bearer token -> not 401
    status = get_status("/mcp", {"Authorization": f"Bearer {TOKEN}"})
    log.info("Case 3 GET /mcp valid bearer -> %s", status)
    assert status != 401, f"did not expect 401, got {status}"

    # Case 4: X-API-Key header -> not 401
    status = get_status("/mcp", {"X-API-Key": TOKEN})
    log.info("Case 4 GET /mcp X-API-Key -> %s", status)
    assert status != 401, f"did not expect 401, got {status}"

    # Case 5: bare token -> not 401
    status = get_status("/mcp", {"Authorization": TOKEN})
    log.info("Case 5 GET /mcp bare token -> %s", status)
    assert status != 401, f"did not expect 401, got {status}"

    # Case 6: case-insensitive scheme -> not 401
    status = get_status("/mcp", {"Authorization": f"bearer {TOKEN}"})
    log.info("Case 6 GET /mcp lowercase bearer -> %s", status)
    assert status != 401, f"did not expect 401, got {status}"

    # Case 7: exempt health paths -> not 401; /health now has a handler.
    status = get_status("/")
    log.info("Case 7 GET / -> %s", status)
    assert status != 401, f"did not expect 401 for /, got {status}"

    status, body = get_body("/health")
    log.info("Case 7 GET /health -> %s", status)
    assert status != 401, f"did not expect 401 for /health, got {status}"
    assert status == 200, f"expected 200 for /health, got {status}"
    assert json.loads(body) == {"status": "ok"}

    # Case 11: 401 body identical for missing vs wrong token (already asserted above)
    log.info("Case 11 401 body verified identical for missing/wrong token")


def start_server(env: dict[str, str]) -> subprocess.Popen[str]:
    log.info("starting server with env %s", {k: "***" if k == "MCP_API_TOKEN" else v for k, v in env.items()})
    # Avoid inheriting a parent MCP_API_TOKEN so the no-auth case is truly token-free.
    base_env = {k: v for k, v in os.environ.items() if k != "MCP_API_TOKEN"}
    with open("/tmp/srv.log", "w") as log_fh:
        proc = subprocess.Popen(
            ["python", "server.py"],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env={**base_env, **env},
            cwd="/app",
            text=True,
        )
    time.sleep(5.0)
    if proc.poll() is not None:
        raise RuntimeError(f"server exited early with code {proc.returncode}")
    return proc


def stop_server(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()


async def test_mcp_client_no_auth() -> None:
    """Case 9: full MCP protocol when auth is disabled."""
    status, body = get_body("/mcp")
    log.info("Case 9a: no-auth GET /mcp -> %s", status)
    assert status != 401, f"did not expect 401 when auth disabled, got {status}"

    log.info("Case 9b: no-auth MCP protocol (24 tools)")
    async with mcp_client_session(None) as session:
        result = await session.list_tools()
        tool_names = [t.name for t in result.tools]
        log.info("no-auth tools: %d", len(tool_names))
        assert len(tool_names) == 24, f"expected 24 tools, got {len(tool_names)}"


async def main() -> int:
    log.info("HTTP auth e2e against %s", BASE_URL)

    proc = start_server({
        "MCP_TRANSPORT": "http",
        "MCP_HOST": os.environ.get("MCP_HOST", "127.0.0.1"),
        "MCP_PORT": os.environ.get("MCP_PORT", "8000"),
        "MCP_API_TOKEN": TOKEN,
    })
    try:
        test_raw_http()
        await test_mcp_client_auth()
    finally:
        stop_server(proc)

    proc = start_server({
        "MCP_TRANSPORT": "http",
        "MCP_HOST": os.environ.get("MCP_HOST", "127.0.0.1"),
        "MCP_PORT": os.environ.get("MCP_PORT", "8000"),
    })
    try:
        await test_mcp_client_no_auth()
    finally:
        stop_server(proc)

    log.info("HTTP AUTH E2E PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        log.exception("HTTP AUTH E2E FAILED")
        raise
