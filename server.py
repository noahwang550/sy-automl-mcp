"""FastMCP entrypoint for sy-automl-mcp.

Registers all tools and selects a transport from the environment:
- ``stdio`` (default): for local Claude Code via ``docker run -i``.
- ``streamable-http``: for remote/shared use behind ``MCP_TRANSPORT=http``.

Run:
    python server.py
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from config import MCP_HOST, MCP_PORT, MCP_TRANSPORT, ensure_dirs
from tools._common import safe_tool
from tools.data import load_dataset, validate_dataset
from tools.model_management import delete_model, list_models, load_model, model_info
from tools.multimodal import evaluate_multimodal, predict_multimodal, train_multimodal
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
):
    mcp.tool()(safe_tool(_fn))


def main() -> None:
    ensure_dirs()
    log.info("Starting sy-automl-mcp (transport=%s)", MCP_TRANSPORT)
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    elif MCP_TRANSPORT in ("http", "streamable-http"):
        mcp.settings.host = MCP_HOST
        mcp.settings.port = MCP_PORT
        mcp.run(transport="streamable-http")
    else:
        raise SystemExit(f"Unknown MCP_TRANSPORT={MCP_TRANSPORT!r} (use stdio|http)")


if __name__ == "__main__":
    main()
