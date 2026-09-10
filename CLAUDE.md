# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Wrap [AutoGluon](https://github.com/autogluon/autogluon) (AWS's open-source AutoML framework) into an **MCP (Model Context Protocol) server** so that AI assistants such as Claude can drive AutoML workflows — loading datasets, training models, predicting, and evaluating — through standard MCP tool calls.

AutoGluon capabilities exposed:
- **TabularPredictor** — classification/regression on structured tables (`fit`, `predict`, `evaluate`, `leaderboard`, `feature_importance`, `fit_summary`).
- **TimeSeriesPredictor** — forecasting (`target`, `prediction_length`, `freq`).
- **MultimodalPredictor** — image, text, and multimodal tasks.

## Tech Stack

- **Language:** Python 3.11 (AutoGluon 1.5.0 verified).
- **MCP server:** `mcp` Python SDK, FastMCP decorator style (`@mcp.tool()`). Transports: `stdio` (default) and `streamable-http`.
- **AutoGluon:** `autogluon.tabular` 1.5.0, `autogluon.timeseries` 1.5.0, `autogluon.multimodal` 1.5.0.
- **Docker:** `python:3.11-slim` base, tiered build (tabular vs full). `pandas` 2.3.3.

## Critical Constraints

- **Docker is the primary runtime**, not native host Python. AutoGluon officially supports Linux/macOS only; this project runs the MCP server inside a Docker container regardless of host OS.
  - `Dockerfile` based on `python:3.11-slim`, tiered install via `--build-arg TIER=tabular|full`.
  - `artifacts/` bind-mounted at `/app/artifacts` for persistence.
  - Image ENTRYPOINT is `python server.py` — override with `--entrypoint sh` or `--entrypoint python` for pytest.
  - `pytest` is NOT in the production image — install at runtime (`pip install pytest pytest-asyncio -q`) before running tests.
  - Tabular tier runs on CPU; full tier benefits from `--gpus all`.
- **Training is long-running.** `fit()` can take minutes to hours. Background-task pattern: `train_*` returns `task_id`, poll via `get_task_status` / `get_task_result`.
- **Heavy dependencies.** AutoGluon + torch pull several GB. Docker isolates this.
- **Stdout must stay clean.** AutoGluon/PyTorch/Lightning write progress bars + banners to stdout/stderr, which would corrupt MCP stdio JSON-RPC. See Stdout Pollution Fix below.
- **Env vars:**
  - `MCP_TRANSPORT` (`stdio` default | `http` for streamable-http)
  - `MCP_HOST` / `MCP_PORT` (default `0.0.0.0` / `8000`)
  - `MCP_API_TOKEN` (default: unset) — Bearer token for streamable-http auth. Unset/empty = auth disabled (backward-compatible, fine for trusted/local use).
  - `MCP_MODEL_CACHE_MAX` (default `4`) — LRU cap for in-memory predictor cache.
  - `MCP_TASK_RETENTION_SECONDS` (default `86400`) — TTL for terminal task records before sweep.
  - `MCP_TASK_MAX_RETAINED` (default `100`) — max terminal tasks retained before oldest-first sweep.
  - `MCP_MAX_WORKERS` (default `1`) — background-task thread pool size. Safe to raise above 1 because stdout/stderr redirection is thread-local (see Stdout section).
  - `MAX_UPLOAD_CHUNK_BYTES` (default `1048576`) — max decoded size of a single `upload_dataset_chunk` payload.
  - `MAX_UPLOAD_TOTAL_BYTES` (default `268435456`) — max assembled size of a `finalize_dataset` output.

## Build & Run Commands

```bash
# Build tabular tier (default)
docker build -t sy-automl-mcp .

# Build full tier (timeseries + multimodal)
docker build -t sy-automl-mcp:full --build-arg TIER=full .

# Run stdio (local Claude Code)
docker run -i --rm -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp

# Run streamable-http
docker run --rm -p 8000:8000 \
  -e MCP_TRANSPORT=http -e MCP_PORT=8000 \
  -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp

# Run tests (pytest not in image; install at runtime)
docker run --rm --entrypoint sh \
  -v "$PWD/artifacts:/app/artifacts" \
  sy-automl-mcp \
  -c "pip install pytest pytest-asyncio -q && python -m pytest tests/ -v"

# Run a single test file / test
docker run --rm --entrypoint sh -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp \
  -c "pip install pytest pytest-asyncio -q && python -m pytest tests/test_auth.py -v"
docker run --rm --entrypoint sh -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp \
  -c "pip install pytest pytest-asyncio -q && python -m pytest tests/test_auth.py::test_name -v"

# Run lint
docker run --rm --entrypoint sh sy-automl-mcp \
  -c "pip install ruff -q && ruff check ."

# Live stdio e2e harness
docker run --rm --entrypoint sh -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp:full \
  -c "pip install mcp -q && python e2e_stdio.py"
```

## Architecture

- `server.py` — FastMCP entrypoint, registers all 28 tools, selects transport from env. Registered tools are wrapped with `safe_tool` (defense-in-depth — converts unhandled exceptions to the unified envelope). When `MCP_TRANSPORT=http` AND `MCP_API_TOKEN` is set: builds the ASGI app via `mcp.streamable_http_app()`, adds `BearerTokenMiddleware`, serves via `uvicorn.run(app, host, port)`. A `_McpOrHealthApp` ASGI wrapper exempts `GET /` and `GET /health` from auth (returns `200 {"status":"ok"}` as an intentionally-unauthed liveness probe). Startup logs "streamable-http auth enabled/disabled" — NEVER the token value. stdio path and no-token http path are unchanged.
- `config.py` — Path constants, env var parsing, registry helpers, ID validation. All artifact paths resolve under a single root (`ARTIFACTS_DIR`); tool code must never accept raw absolute paths from callers — it resolves user-supplied identifiers against this root and rejects traversal attempts (`validate_id`).
- `tools/` — One module per capability group: `tabular.py`, `timeseries.py`, `multimodal.py`, `model_management.py`, `data.py`, `task_status.py`, `_common.py`, `auth.py`.
  - `auth.py` exports `check_bearer_token(auth_header, expected) -> bool` (constant-time `secrets.compare_digest` for ALL header paths — missing, wrong, and correct tokens all take the same comparison branch) and `BearerTokenMiddleware` (Starlette `BaseHTTPMiddleware`). Accepted headers: `Authorization: Bearer <token>` (case-insensitive scheme), `X-API-Key: <token>`, bare `<token>` in `Authorization`. Returns generic `401 {"detail":"Unauthorized"}` — no token echo, no missing-vs-wrong distinction. Exempts `GET /` and `GET /health` (strict: method GET + exact path).
  - `_common.py` installs a process-wide `_ThreadLocalOutputProxy` on `sys.stdout`/`sys.stderr` at import and provides `_suppress_output()` (sets the thread-local target to `os.devnull`) plus `set_thread_output_target()` / `reset_thread_output_target()` helpers. Special methods (`__iter__`, `__next__`, …) are implemented explicitly on the proxy class because Python looks them up on the type, not via `__getattr__`. Also exports `safe_tool`, a decorator applied to every public tool so that any unhandled exception is converted to a failure envelope (the MCP layer never sees a raw exception).
  - `model_management.py` holds a thread-safe `_ModelLRUCache` (OrderedDict, move-to-end, popitem(last=False)) capped by `MCP_MODEL_CACHE_MAX`. Exposes `get_or_load()` which serializes concurrent loads of the same uncached key via a per-cache lock + double-checked loading.
  - `multimodal.py` validates image-column values via `_resolve_image_path()` — rejects absolute paths, resolves relative paths against `ARTIFACTS_DIR`, and raises `ValueError` if the resolved path escapes the artifacts root (path-traversal mitigation).
  - `data.py` exposes `load_dataset` (URL / inline text / pre-mounted filename) plus a chunked-upload escape hatch for attachments that live in a separate storage domain from the container (e.g. agent-platform session attachments): `upload_dataset_chunk(dataset_id, chunk_index, total_chunks, content_base64, format)` writes one base64 chunk to `artifacts/datasets/<id>/.chunks/` and tracks progress in `meta.json`; `finalize_dataset(dataset_id, format)` concatenates chunks 0..N-1 in order, atomically replaces `data.<ext>`, applies `_enforce_size_limits`, removes the `.chunks/` dir, and returns the same envelope shape as `load_dataset`. Chunks may arrive out of order and concurrently; `total_chunks` must not change mid-upload. Use this path when the source exceeds ~256KB and cannot be fetched via http(s).
- `tasks/` — Background task manager: `manager.py` (ThreadPoolExecutor), `registry.py` (task_id → Task records), `progress.py` (best-effort AutoGluon log parser).
  - `manager.py` redirects stdout/stderr to the task log file during background execution via `set_thread_output_target()` (thread-local, safe at `max_workers > 1`). Task logs do NOT include full Python tracebacks on failure — only the exception message is written to the user-facing FAILED line (traceback details are not exposed via `get_task_status` / `log_tail`). `TaskManager.status()` attaches a `progress` field to the status dict (populated by `progress.parse_progress()`).
  - `registry.py` uses a module-level `threading.RLock()` (re-entrant — `sweep()` re-enters store operations that take the lock), per-task `_state_lock`, sticky terminal states (SUCCESS/FAILED/CANCELLED — a cancel arriving after completion returns `already_terminal` instead of overwriting), and a `sweep()` that runs on `add`/`get`/`list`/`snapshot`/`require` to evict terminal tasks older than `MCP_TASK_RETENTION_SECONDS` or over the `MCP_TASK_MAX_RETAINED` cap. Running/pending tasks are never evicted. Looking up an evicted id raises a clear "Task expired or not found" which callers catch (not a crash).
  - `progress.py` exports `parse_progress(log_path, status)` — best-effort parses the AutoGluon task log into a structured dict (`announced_models`, `models_attempted`, `latest_score`, `latest_model`, `metric`, `recent_lines`); never raises. Reports *latest* score (not a claimed "best") because metric direction is metric-dependent.
- `serialization/` — `envelope.py` (unified `{success, data, error}` response), `dataframe.py` (DataFrame → JSON-serializable dicts/lists).
- `artifacts/` — Runtime directory for datasets, models, predictions (bind-mounted, gitignored).
- `e2e_stdio.py` — Live stdio MCP round-trip harness at repo root. Spawns the server via the `mcp` SDK, asserts 28 tools are listed, and drives a full tabular flow end-to-end; asserts stdout stays clean.

## Streamable-HTTP Bearer Token Auth

When `MCP_API_TOKEN` is set and `MCP_TRANSPORT=http`, all HTTP requests to the MCP endpoint must present the token. stdio transport is completely unaffected (inherently private — single process, no network).

- Timing-safe comparison via `secrets.compare_digest` for ALL header paths (no timing oracle).
- Generic `401 {"detail":"Unauthorized"}` — no token echo, no missing-vs-wrong distinction.
- `GET /` and `GET /health` are exempted from auth (liveness/readiness probes; strict match on method + path).
- Startup log says "streamable-http auth enabled" or "disabled" — NEVER logs the token value.
- Accepted header formats: `Authorization: Bearer <token>` (case-insensitive scheme), `X-API-Key: <token>`, bare `<token>` in `Authorization`.

When `MCP_API_TOKEN` is unset or empty, the http transport works exactly as before (no auth). This is the default and is appropriate for trusted/local networks.

## Stdout Pollution Fix (IMPORTANT)

AutoGluon/PyTorch/Lightning write progress bars + banners to stdout/stderr, which would corrupt MCP stdio JSON-RPC. Fixed with a thread-local proxy + two-layer defense:

1. `tools/_common.py`: installs `_ThreadLocalOutputProxy` on `sys.stdout` / `sys.stderr` once at import. `_suppress_output()` sets the current thread's target to `os.devnull` around every inline `envelope_call`.
2. `tasks/manager.py`: background worker calls `set_thread_output_target(task_log_fh)` while `func(task)` runs, then `reset_thread_output_target()` — only the worker thread's writes are redirected; other threads are unaffected.
3. `verbosity=0` on supported AutoGluon constructors/methods. **Note:** AutoGluon 1.5.0 — `evaluate()`, `feature_importance()`, `predict()`, `leaderboard()` do NOT accept `verbosity` (no `**kwargs`); pass it only on constructors, `fit()`, `fit_summary()`.

Output redirection is thread-safe at `max_workers > 1` (thread-local targets, global proxy is read-only after install). Safe to raise `MCP_MAX_WORKERS` for parallel training.

## Known Limitations

- Training `fit()` can run for a long time; `cancel_task` is a **soft cancel** (cannot hard-kill a thread). Actual interruption relies on `time_limit` — always set a reasonable one.
- streamable-http can be auth-gated via `MCP_API_TOKEN` (Bearer); when unset it remains unauthenticated (trusted networks only). stdio is inherently private (no auth needed).
- Windows-native Python execution is not supported.

## Production Deployment

For remote / multi-agent deployment (nginx + TLS + fail2ban + multi-token auth + Docker compose + systemd), see [`DEPLOY_PLAN.md`](./DEPLOY_PLAN.md). That plan also covers the multi-agent auth extension to `tools/auth.py` (token table + `agent_id` injection + audit logging + hot reload), which supersedes the single-token `MCP_API_TOKEN` mechanism when multiple agent platforms need to be identified separately.

## Session History

Detailed engineering rounds, code review results, and verification checklists are recorded in [`PROGRESS.md`](./PROGRESS.md) and git history. CLAUDE.md intentionally omits session-level progress to stay focused on durable guidance.
