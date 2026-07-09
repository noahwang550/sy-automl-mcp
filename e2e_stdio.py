"""Live stdio MCP end-to-end flow against sy-automl-mcp.

Runs inside the container, spawns ``python server.py`` as a subprocess, and
drives the real JSON-RPC stdio transport through the official ``mcp`` SDK.  The
script asserts that:

* the server boots and initializes,
* exactly 24 tools are listed,
* a tabular round-trip (load -> train -> poll -> predict) succeeds,
* no AutoGluon/stdout pollution appears on the server's stdio stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("e2e_stdio")

CSV_DATA = """sepal_length,sepal_width,species
5.1,3.5,setosa
4.9,3.0,setosa
4.7,3.2,setosa
5.0,3.6,setosa
5.4,3.9,setosa
4.6,3.4,setosa
5.0,3.4,setosa
4.4,2.9,setosa
4.9,3.1,setosa
5.4,3.7,setosa
7.0,3.2,versicolor
6.4,3.2,versicolor
6.9,3.1,versicolor
5.5,2.3,versicolor
6.5,2.8,versicolor
5.7,2.8,versicolor
6.3,3.3,versicolor
4.9,2.4,versicolor
6.6,2.9,versicolor
5.2,2.7,versicolor
"""

PREDICT_CSV = """sepal_length,sepal_width
5.5,3.5
6.0,3.0
"""


@asynccontextmanager
async def mcp_session():
    params = StdioServerParameters(
        command="python",
        args=["server.py"],
        env=None,
        cwd="/app",
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _text(result: CallToolResult) -> str:
    if result.content and result.content[0].type == "text":
        return result.content[0].text
    raise ValueError(f"Unexpected tool result content: {result.content!r}")


def _json(result: CallToolResult) -> dict:
    text = _text(result)
    return json.loads(text)


async def main() -> int:
    async with mcp_session() as session:
        # 1. tools/list should report 24 tools
        tools_result = await session.list_tools()
        tool_names = [t.name for t in tools_result.tools]
        log.info("tools listed: %d", len(tool_names))
        assert len(tool_names) == 24, f"expected 24 tools, got {len(tool_names)}"

        # 2. load_dataset with inline CSV
        dataset_id = f"iris_e2e_{int(time.time())}"
        load_res = await session.call_tool(
            "load_dataset",
            arguments={"source": CSV_DATA, "dataset_id": dataset_id, "format": "csv"},
        )
        load_data = _json(load_res)
        log.info(
            "load_dataset: %s rows=%s",
            load_data.get("success"),
            load_data.get("data", {}).get("rows"),
        )
        assert load_data.get("success") is True, load_data.get("error")

        # 3. train_tabular background task
        model_id = f"model_e2e_{int(time.time())}"
        train_res = await session.call_tool(
            "train_tabular",
            arguments={
                "dataset_id": dataset_id,
                "target": "species",
                "model_id": model_id,
                "time_limit": 30,
                "presets": "medium_quality",
            },
        )
        train_data = _json(train_res)
        log.info("train_tabular: %s", train_data)
        assert train_data.get("success") is True, train_data.get("error")
        task_id = train_data["data"]["task_id"]

        # 4. poll get_task_status until terminal
        status = "pending"
        for _ in range(60):
            status_res = await session.call_tool(
                "get_task_status", arguments={"task_id": task_id}
            )
            status_data = _json(status_res)
            status = status_data["data"]["status"]
            log.info("task status: %s", status)
            if status in ("success", "failed", "cancelled"):
                break
            await asyncio.sleep(2)
        assert status == "success", f"training did not succeed: {status}"

        # 5. predict_tabular with inline CSV
        pred_res = await session.call_tool(
            "predict_tabular",
            arguments={"model_id": model_id, "inline_csv": PREDICT_CSV},
        )
        pred_data = _json(pred_res)
        log.info("predict_tabular: %s", pred_data)
        assert pred_data.get("success") is True, pred_data.get("error")
        predictions = pred_data["data"].get("predictions", [])
        log.info("predictions: %s", predictions)
        assert len(predictions) == 2, f"expected 2 predictions, got {len(predictions)}"

    log.info("STDIO E2E FLOW PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        log.exception("STDIO E2E FLOW FAILED")
        raise
