"""Agent-platform helper: upload a local file to sy-automl-mcp via chunked upload.

Use this when the source file lives in a storage domain the MCP container
cannot reach (e.g. agent-platform session attachments exposed only via
``artifact://``). The helper splits the file into base64 chunks under the
MCP single-request size limit, calls ``upload_dataset_chunk`` N times, then
``finalize_dataset`` to assemble them on the server. The returned
``dataset_id`` is then usable with ``train_tabular`` / ``predict_tabular`` /
etc.

USAGE (library) ::

    from scripts.upload_file import upload_file_as_dataset
    dataset_id = upload_file_as_dataset(
        path="/path/to/data.csv",
        dataset_id="my_data",
        mcp_url="http://10.0.0.5:9885/mcp",
        mcp_token="xxxx",
        format="csv",
    )

USAGE (CLI) ::

    MCP_URL=http://10.0.0.5:9885/mcp MCP_TOKEN=xxxx \\
        python scripts/upload_file.py /path/to/data.csv my_data --format csv
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Keep each chunk comfortably under typical MCP single-request size limits
# (~1MB). 256KiB raw -> ~342KiB base64, leaving headroom for JSON envelope.
CHUNK_BYTES = 256 * 1024


async def _call(session: ClientSession, tool: str, args: dict) -> dict:
    import json

    result = await session.call_tool(tool, arguments=args)
    if not result.content or result.content[0].type != "text":
        raise RuntimeError(f"tool {tool} returned no text content: {result.content!r}")
    payload = json.loads(result.content[0].text)
    if not payload.get("success"):
        raise RuntimeError(f"tool {tool} failed: {payload.get('error')}")
    return payload["data"]


async def _upload_file_async(
    path: Path,
    dataset_id: str,
    mcp_url: str,
    mcp_token: str | None,
    fmt: str,
    chunk_bytes: int = CHUNK_BYTES,
) -> dict:
    raw = path.read_bytes()
    total = len(raw)
    n_chunks = max(1, (total + chunk_bytes - 1) // chunk_bytes)
    print(f"uploading {total} bytes as {n_chunks} chunk(s) -> dataset_id={dataset_id}", file=sys.stderr)

    headers = {"Authorization": f"Bearer {mcp_token}"} if mcp_token else {}
    async with streamablehttp_client(mcp_url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            for i in range(n_chunks):
                chunk = raw[i * chunk_bytes : (i + 1) * chunk_bytes]
                b64 = base64.b64encode(chunk).decode("ascii")
                data = await _call(session, "upload_dataset_chunk", {
                    "dataset_id": dataset_id,
                    "chunk_index": i,
                    "total_chunks": n_chunks,
                    "content_base64": b64,
                    "format": fmt,
                })
                print(f"  chunk {i+1}/{n_chunks}: {data['bytes_received']} bytes", file=sys.stderr)

            summary = await _call(session, "finalize_dataset", {
                "dataset_id": dataset_id,
                "format": fmt,
            })
            print(f"finalized: {summary['rows']} rows, {len(summary['columns'])} cols", file=sys.stderr)
            return summary


def upload_file_as_dataset(
    path: str | Path,
    dataset_id: str,
    mcp_url: str | None = None,
    mcp_token: str | None = None,
    format: str = "auto",
    chunk_bytes: int = CHUNK_BYTES,
) -> dict:
    """Upload a local file to the MCP server and return the finalized summary.

    Reads MCP_URL and MCP_TOKEN from env if not passed explicitly.
    """
    mcp_url = mcp_url or os.environ.get("MCP_URL")
    mcp_token = mcp_token or os.environ.get("MCP_TOKEN")
    if not mcp_url:
        raise ValueError("MCP_URL is required (env var or arg)")
    return asyncio.run(_upload_file_async(
        Path(path), dataset_id, mcp_url, mcp_token, format, chunk_bytes
    ))


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload a local file to sy-automl-mcp via chunked upload.")
    ap.add_argument("path", help="Path to the local data file")
    ap.add_argument("dataset_id", help="Unique dataset id (letters/digits/_ . -)")
    ap.add_argument("--format", default="auto", choices=["auto", "csv", "parquet", "json"])
    ap.add_argument("--mcp-url", default=os.environ.get("MCP_URL"))
    ap.add_argument("--mcp-token", default=os.environ.get("MCP_TOKEN"))
    ap.add_argument("--chunk-bytes", type=int, default=CHUNK_BYTES)
    args = ap.parse_args()

    summary = upload_file_as_dataset(
        args.path, args.dataset_id,
        mcp_url=args.mcp_url, mcp_token=args.mcp_token,
        format=args.format, chunk_bytes=args.chunk_bytes,
    )
    import json
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
