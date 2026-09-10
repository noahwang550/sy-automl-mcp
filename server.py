"""FastMCP entrypoint for sy-automl-mcp.

Registers all tools and selects a transport from the environment:
- ``stdio`` (default): for local Claude Code via ``docker run -i``.
- ``streamable-http``: for remote/shared use behind ``MCP_TRANSPORT=http``.

Run:
    python server.py
"""
from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from config import (
    MCP_API_TOKEN,
    MCP_API_TOKENS_FILE,
    MCP_DOWNLOAD_ENABLED,
    MCP_HOST,
    MCP_PORT,
    MCP_TRANSPORT,
    MCP_UPLOAD_ENABLED,
    ensure_dirs,
)
from tools._common import safe_tool
from tools.artifacts import (
    download_handler,
    get_artifact_bytes,
    get_artifact_chunk,
    get_artifact_url,
    list_artifacts,
)
from tools.auth import BearerTokenMiddleware
from tools.data import (
    finalize_dataset,
    load_dataset,
    upload_dataset,
    upload_dataset_chunk,
    validate_dataset,
)
from tools.model_management import delete_model, list_models, load_model, model_info
from tools.multimodal import evaluate_multimodal, predict_multimodal, train_multimodal
from tools.report import get_training_report
from tools.tabular import (
    evaluate_tabular,
    feature_importance_tabular,
    fit_summary_tabular,
    leaderboard_tabular,
    predict_tabular,
    train_tabular,
)
from tools.task_status import cancel_task, get_task_result, get_task_status, list_tasks
from tools.timeseries import (
    evaluate_timeseries,
    fit_summary_timeseries,
    leaderboard_timeseries,
    predict_timeseries,
    train_timeseries,
)
from tools.upload import (
    check_upload_token,
    get_upload_instructions,
    handle_get as upload_get,
    handle_post as upload_post,
    unauthorized_response as upload_unauthorized,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sy-automl-mcp")

mcp = FastMCP("autogluon-mcp")


# -- Registration -----------------------------------------------------------
# Each tool function carries its own type hints + docstring; FastMCP derives
# the JSON schema and description from them. We register them explicitly so
# the tool names match the plan's contract.
for _fn in (
    # data
    load_dataset,
    validate_dataset,
    upload_dataset,
    upload_dataset_chunk,
    finalize_dataset,
    get_upload_instructions,
    # tabular
    train_tabular,
    predict_tabular,
    leaderboard_tabular,
    feature_importance_tabular,
    fit_summary_tabular,
    evaluate_tabular,
    # timeseries
    train_timeseries,
    predict_timeseries,
    leaderboard_timeseries,
    evaluate_timeseries,
    fit_summary_timeseries,
    # multimodal
    train_multimodal,
    predict_multimodal,
    evaluate_multimodal,
    # model management
    list_models,
    load_model,
    model_info,
    delete_model,
    # tasks
    get_task_status,
    get_task_result,
    cancel_task,
    list_tasks,
    # v0.6.0: training report + artifact download bridge
    get_training_report,
    list_artifacts,
    get_artifact_url,
    # v0.6.1: inline-bytes bridge (bypasses presigned URLs when platform
    # auto-redacts token query strings)
    get_artifact_bytes,
    # v0.6.2: chunked-pull (small slices for large files under platform
    # tool-response redaction thresholds)
    get_artifact_chunk,
):
    mcp.tool()(safe_tool(_fn))


class _McpOrHealthApp:
    """ASGI wrapper that serves a stateless /health probe, an optional
    browser upload page, and an optional /download artifact bridge before
    the MCP app handles JSON-RPC."""

    def __init__(self, mcp_app) -> None:
        self.mcp_app = mcp_app
        self._health = JSONResponse({"status": "ok"})
        self._upload_enabled = MCP_UPLOAD_ENABLED
        self._download_enabled = MCP_DOWNLOAD_ENABLED

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.mcp_app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method == "GET" and path == "/health":
            await self._health(scope, receive, send)
            return
        if method == "GET" and path == "/":
            await self._health(scope, receive, send)
            return
        if self._upload_enabled and path == "/upload":
            agent_id = check_upload_token(scope)
            if agent_id is None:
                await upload_unauthorized()(scope, receive, send)
                return
            token = ""
            if agent_id != "anonymous":
                from tools.upload import _extract_token
                token = _extract_token(scope)
            if method == "GET":
                await upload_get(scope, receive, send, token)
                return
            if method == "POST":
                await upload_post(scope, receive, send, token)
                return
        if self._download_enabled and (
            path == "/download"
            or path.startswith("/download/")
            or path.startswith("/d/")
        ):
            await download_handler(scope, receive, send)
            return
        await self.mcp_app(scope, receive, send)


def main() -> None:
    ensure_dirs()
    log.info("Starting sy-automl-mcp (transport=%s)", MCP_TRANSPORT)
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    elif MCP_TRANSPORT in ("http", "streamable-http"):
        mcp.settings.host = MCP_HOST
        mcp.settings.port = MCP_PORT
        mcp_app = mcp.streamable_http_app()
        app: object = _McpOrHealthApp(mcp_app)
        if MCP_API_TOKENS_FILE or MCP_API_TOKEN:
            log.info(
                "streamable-http auth enabled (multi-token=%s)",
                bool(MCP_API_TOKENS_FILE),
            )
            app = BearerTokenMiddleware(
                app,
                tokens_file=MCP_API_TOKENS_FILE,
                legacy_token=MCP_API_TOKEN,
                exempt_paths_all_methods=(
                    ({"/upload"} if MCP_UPLOAD_ENABLED else set()) | {"/download"}
                ),
                # v0.6.4: short session_id URLs and legacy path-token URLs
                # carry their own auth (HMAC signature looked up server-side).
                # Bearer middleware must not gate these — the agent platform
                # can't attach Authorization headers to a browser-clicked
                # URL, and the short session_id IS the auth.
                exempt_path_prefixes={"/d/", "/download/"},
            )
        else:
            log.info("streamable-http auth disabled")
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
    else:
        raise SystemExit(f"Unknown MCP_TRANSPORT={MCP_TRANSPORT!r} (use stdio|http)")


if __name__ == "__main__":
    main()
