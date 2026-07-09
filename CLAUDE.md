# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Phase 1 and Phase 2 both verified. Phase 3 partially complete.**

- **Tabular (Phase 1):** ✅ End-to-end verified via live MCP server stdio flow: `load_dataset` (inline CSV) → `train_tabular` (returns task_id) → poll `get_task_status` → `predict_tabular` (returns predictions). Test suite on `:latest`: 31 passed, 2 skipped (TS/MM skip expected — they need `:full`).
- **TimeSeries + Multimodal (Phase 2):** ✅ **VERIFIED against real AutoGluon in `sy-automl-mcp:full`.** Full test suite: **33 passed, 0 skipped, 0 failed** (~2 min). All 10 checklist items PASS or FIXED. 5 additional bugs found and fixed in this final pass (see below).
- **Phase 3 (hardening):** Partially done. Unified error envelope ✅, resource limits ✅, stdout pollution fix ✅. Progress parsing ❌, CI lint pipeline ❌, 80% test coverage ❌.
- **Git:** Initialized, files staged, **zero commits**. Remote `git@github.com:noahwang550/sy-automl-mcp.git` not yet configured.

## Project Purpose

Wrap [AutoGluon](https://github.com/autogluon/autogluon) (AWS's open-source AutoML framework) into an **MCP (Model Context Protocol) server** so that AI assistants such as Claude can drive AutoML workflows — loading datasets, training models, predicting, and evaluating — through standard MCP tool calls.

AutoGluon capabilities exposed:
- **TabularPredictor** — classification/regression on structured tables (`fit`, `predict`, `evaluate`, `leaderboard`, `feature_importance`, `fit_summary`). ✅ Verified (`:latest`)
- **TimeSeriesPredictor** — forecasting (`target`, `prediction_length`, `freq`). ✅ Verified (`:full`)
- **MultimodalPredictor** — image, text, and multimodal tasks. ✅ Verified (`:full`)

## Tech Stack

- **Language:** Python 3.11 (AutoGluon 1.5.0 verified).
- **MCP server:** `mcp` Python SDK, FastMCP decorator style (`@mcp.tool()`). Transports: `stdio` (default) and `streamable-http`.
- **AutoGluon:** `autogluon.tabular` 1.5.0, `autogluon.timeseries` 1.5.0, `autogluon.multimodal` 1.5.0.
- **Docker:** `python:3.11-slim` base, tiered build (tabular vs full). `pandas` 2.3.3.

## Critical Constraints

- **Windows host, Docker runtime.** AutoGluon's official support is Linux and macOS. **This project runs the MCP server inside a Docker container** — Docker is the primary runtime, not WSL2 or native Windows Python.
- **Docker conventions:**
  - `Dockerfile` based on `python:3.11-slim`, tiered install via `--build-arg TIER=tabular|full`.
  - `artifacts/` bind-mounted at `/app/artifacts` for persistence.
  - Image ENTRYPOINT is `python server.py` — override with `--entrypoint sh` or `--entrypoint python` for pytest.
  - `pytest` is NOT in the production image — install at runtime (`pip install pytest pytest-asyncio -q`) before running tests.
  - Tabular tier runs on CPU; full tier benefits from `--gpus all`.
- **Git Bash (Windows):** Prefix `MSYS_NO_PATHCONV=1` on `docker run -w /app` to prevent path mangling.
- **Training is long-running.** `fit()` can take minutes to hours. Background-task pattern: `train_*` returns `task_id`, poll via `get_task_status` / `get_task_result`.
- **Heavy dependencies.** AutoGluon + torch pull several GB. Docker isolates this.

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

# Run lint
docker run --rm --entrypoint sh sy-automl-mcp \
  -c "pip install ruff -q && ruff check ."
```

## Architecture

- `server.py` — FastMCP entrypoint, registers all 24 tools, selects transport from env.
- `config.py` — Path constants, env var parsing, registry helpers, ID validation.
- `tools/` — One module per capability group: `tabular.py`, `timeseries.py`, `multimodal.py`, `model_management.py`, `data.py`, `task_status.py`, `_common.py`.
  - `_common.py` provides `_suppress_output()` context manager that redirects stdout/stderr to `os.devnull` during inline calls.
- `tasks/` — Background task manager: `manager.py` (ThreadPoolExecutor, default `max_workers=1`), `registry.py` (task_id → Task records).
  - `manager.py` redirects stdout/stderr to the task log file during background execution.
- `serialization/` — `envelope.py` (unified `{success, data, error}` response), `dataframe.py` (DataFrame → JSON-serializable dicts/lists).
- `artifacts/` — Runtime directory for datasets, models, predictions (bind-mounted, gitignored).

## Stdout Pollution Fix (IMPORTANT)

AutoGluon/PyTorch/Lightning write progress bars + banners to stdout/stderr, which would corrupt MCP stdio JSON-RPC. **Confirmed real, now fixed with TWO-LAYER defense:**

1. `tools/_common.py`: `_suppress_output()` redirects `sys.stdout`/`sys.stderr` to `os.devnull` around every inline `envelope_call`.
2. `tasks/manager.py`: background worker redirects `sys.stdout`/`sys.stderr` to the task log file while `func(task)` runs.
3. `verbosity=0` on supported AutoGluon constructors/methods.

**Verified:** live stdio MCP test against `:full` showed ONLY valid JSON-RPC frames — no AutoGluon leakage.

**Thread-safety caveat:** Global stdout/stderr redirect is NOT thread-safe for concurrent workers. Safe with default `MCP_MAX_WORKERS=1` (serial) and sequential stdio handling. Would need per-worker redirection (per-thread `io.StringIO` or logging-based capture) if `max_workers > 1`.

## Code Review Results (latest session)

- **0 CRITICAL, 7 HIGH (all fixed), 3 MEDIUM (deferred), 2 LOW (noted).**
- Verdict: **APPROVE with fixes.**
- HIGH fixes applied: `freq` moved to TimeSeriesPredictor constructor; `predict_timeseries` no-dataset fallback; `reset_index()` for TS predictions; `evaluate_timeseries` honors id/time columns + metrics; multimodal validates image/text columns + file existence; `evaluate_multimodal` forwards metrics; `predict_tabular` enforces exactly-one-of dataset_id/inline_csv; `evaluate_tabular` uses first metric.

## Deferred Tech Debt (4 items — Phase 3)

1. **Soft-cancel status race** in `tasks/manager.py` + `registry.py` — needs per-task lock to prevent cancel/status update race condition.
2. **Unbounded predictor cache** in `tools/model_management.py` — `load_model` cache needs LRU eviction.
3. **TaskStore never evicts** completed tasks — needs TTL/retention policy.
4. **Stdout redirect not thread-safe** — global `sys.stdout`/`sys.stderr` redirect breaks with `max_workers > 1`; needs per-worker capture (per-thread `io.StringIO` or logging-based).

## Additional Bugs Found + Fixed in `:full` Verification Pass

1. `tools/multimodal.py` — missing `from pathlib import Path` (used in image validation). Fixed.
2. `tools/multimodal.py` — `_VALID_PROBLEM_TYPES` too narrow; `"text_classification"` unsupported, `"classification"` literal caused failures. Expanded set; `"multimodal"` and `"classification"` map to `None` so AutoGluon infers. Fixed.
3. `tests/test_timeseries.py` — 3 rows/series was too few (TimeSeriesPredictor needs >=7, filtered to 0). Now 9 daily obs x 2 items. Fixed.
4. `tests/test_multimodal.py` — `"text_classification"` unsupported + only 4 samples -> "No model available". Changed to `problem_type="binary"`, 20 samples, `presets="medium_quality"`, `time_limit=60`. Fixed.
5. `tools/timeseries.py` / `multimodal.py` / `tabular.py` — `verbosity=0` passed to methods that don't accept it (leaderboard, predict, evaluate, MM fit). Removed from those; kept on constructors/fit/fit_summary/feature_importance. Fixed.

## Verification Checklist for `:full` Image — ALL PASS/FIXED ✅

All 10 items verified against real AutoGluon 1.5.0 in `sy-automl-mcp:full`:

1. ✅ TS `freq` in constructor produces expected frequency inference.
2. ✅ TS no-dataset predict fallback correctly reloads training dataset from registry.
3. ✅ TS output shape after `reset_index()` includes item_id/timestamp.
4. ✅ TS evaluate with non-default id/time columns.
5. ✅ TS evaluate accepts metrics list.
6. ✅ **TS/Multimodal stdout pollution** — confirmed real, fixed with two-layer defense (see above). Verified: no leakage on stdio.
7. ✅ Multimodal `problem_type=None` auto-detection (via expanded `_VALID_PROBLEM_TYPES` + `None` mapping).
8. ✅ Multimodal image path validation (absolute + relative-to-ARTIFACTS_DIR). Fixed missing `pathlib` import.
9. ✅ Multimodal `evaluate(metrics=list)` acceptance.
10. ✅ Unknown kwargs pattern — `verbosity=0` removed from methods that don't accept it; kept on supported constructors/fit/fit_summary/feature_importance.

## File Inventory

```
Root: server.py, config.py, Dockerfile, Dockerfile.test, docker-compose.yml,
      pyproject.toml, requirements.txt, requirements-full.txt, requirements-dev.txt,
      CLAUDE.md, PLAN.md, README.md, PROGRESS.md, .gitignore, .dockerignore,
      .python-version, .github/workflows/docker.yml

tools/: tabular.py, timeseries.py, multimodal.py, model_management.py,
        data.py, task_status.py, _common.py

tasks/: manager.py, registry.py

serialization/: envelope.py, dataframe.py

tests/: test_tabular.py, test_timeseries.py, test_multimodal.py,
        test_model_management.py, test_envelope.py, test_serialization.py,
        test_data.py, test_tasks.py, conftest.py
```
