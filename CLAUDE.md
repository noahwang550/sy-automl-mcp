# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Phase 1, Phase 2, and Phase 3 all COMPLETE. v0.2.0 released.**

- **Tabular (Phase 1):** ✅ End-to-end verified via live MCP server stdio flow: `load_dataset` (inline CSV) → `train_tabular` (returns task_id) → poll `get_task_status` → `predict_tabular` (returns predictions).
- **TimeSeries + Multimodal (Phase 2):** ✅ **VERIFIED against real AutoGluon in `sy-automl-mcp:full`.** All 10 checklist items PASS or FIXED. 5 additional bugs found and fixed during Phase 2 verification.
- **Phase 3 (hardening):** ✅ **COMPLETE.** All 4 previously-deferred tech-debt items resolved (cancel race, LRU cache, task retention, thread-safe stdout). Unified error envelope ✅, resource limits ✅, stdout pollution fix ✅, thread-safe output redirection ✅. Remaining optional work (not blockers): progress parsing, CI lint pipeline, 80% test coverage.
- **Test counts (v0.2.0):**
  - `:latest` suite — **52 passed, 2 skipped** (skips = TimeSeries/Multimodal not installed on tabular tier, expected).
  - `:full` suite — **56 passed, 0 skipped, 0 failed** (~2.5 min).
  - Live stdio MCP e2e in `:full` — **PASSED**: 24 tools listed; full tabular round-trip `load_dataset → train_tabular → poll→success → predict_tabular → ['setosa','versicolor']`; stdout clean (no AutoGluon leakage through the thread-local proxy).
- **Git:** Initial commit (`6134717`) plus v0.2.0 release commit. Remote `origin` = `https://github.com/noahwang550/sy-automl-mcp.git` (HTTPS + GCM). Tags `v0.1.0` and `v0.2.0` pushed.

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
- **New env vars (v0.2.0):**
  - `MCP_MODEL_CACHE_MAX` (default `4`) — LRU cap for in-memory predictor cache (`tools/model_management.py`).
  - `MCP_TASK_RETENTION_SECONDS` (default `86400`) — TTL for terminal task records before sweep.
  - `MCP_TASK_MAX_RETAINED` (default `100`) — max number of terminal tasks retained before oldest-first sweep.
  - `MCP_MAX_WORKERS` (default `1`) — background-task thread pool size. Now safe to raise above 1 because stdout/stderr redirection is thread-local (see Stdout section).

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

- `server.py` — FastMCP entrypoint, registers all 24 tools, selects transport from env. Registered tools are wrapped with `safe_tool` (defense-in-depth — guarantees the unified envelope even if a tool raises before `envelope_call`).
- `config.py` — Path constants, env var parsing (incl. `MCP_MODEL_CACHE_MAX`, `MCP_TASK_RETENTION_SECONDS`, `MCP_TASK_MAX_RETAINED`, `MCP_MAX_WORKERS`), registry helpers, ID validation.
- `tools/` — One module per capability group: `tabular.py`, `timeseries.py`, `multimodal.py`, `model_management.py`, `data.py`, `task_status.py`, `_common.py`.
  - `_common.py` installs a process-wide `_ThreadLocalOutputProxy` on `sys.stdout`/`sys.stderr` at import and provides `_suppress_output()` (sets the thread-local target to `os.devnull`) plus `set_thread_output_target()` / `reset_thread_output_target()` helpers. Special methods (`__iter__`, `__next__`, …) are implemented explicitly on the proxy class because Python looks them up on the type, not via `__getattr__`. Also exports `safe_tool`, a decorator applied to every public tool so that any unhandled exception is converted to a failure envelope (the MCP layer never sees a raw exception).
  - `model_management.py` holds a thread-safe `_ModelLRUCache` (OrderedDict, move-to-end, popitem(last=False)) capped by `MCP_MODEL_CACHE_MAX`. Exposes `get_or_load()` which serializes concurrent loads of the same uncached key via a per-cache lock + double-checked loading (resolves the duplicate-load race previously noted as a benign limitation).
  - `multimodal.py` validates image-column values via `_resolve_image_path()` — rejects absolute paths, resolves relative paths against `ARTIFACTS_DIR`, and raises `ValueError` if the resolved path escapes the artifacts root (path-traversal mitigation).
- `tasks/` — Background task manager: `manager.py` (ThreadPoolExecutor, default `max_workers=1`), `registry.py` (task_id → Task records).
  - `manager.py` redirects stdout/stderr to the task log file during background execution via `set_thread_output_target()` (thread-local, safe at `max_workers > 1`). CANCELLED-before-execution branch now sets `finished_at` (terminal tasks always carry a completion timestamp). Task logs no longer include full Python tracebacks on failure — only the exception message is written to the user-facing FAILED line (traceback details are not exposed via `get_task_status` / `log_tail`).
  - `registry.py` uses a module-level `threading.RLock()` (re-entrant — `sweep()` re-enters store operations that take the lock), per-task `_state_lock`, sticky terminal states (SUCCESS/FAILED/CANCELLED — a cancel arriving after completion returns `already_terminal` instead of overwriting), and a `sweep()` that runs on `add`/`get`/`list`/`snapshot`/`require` to evict terminal tasks older than `MCP_TASK_RETENTION_SECONDS` or over the `MCP_TASK_MAX_RETAINED` cap. Running/pending tasks are never evicted. Looking up an evicted id raises a clear "Task expired or not found" which callers catch (not a crash).
- `serialization/` — `envelope.py` (unified `{success, data, error}` response), `dataframe.py` (DataFrame → JSON-serializable dicts/lists).
- `artifacts/` — Runtime directory for datasets, models, predictions (bind-mounted, gitignored).
- `e2e_stdio.py` — Live stdio MCP round-trip harness at repo root. Spawns the server via the `mcp` SDK, asserts 24 tools are listed, and drives a full tabular flow end-to-end; asserts stdout stays clean (no AutoGluon leakage).

## Stdout Pollution Fix (IMPORTANT)

AutoGluon/PyTorch/Lightning write progress bars + banners to stdout/stderr, which would corrupt MCP stdio JSON-RPC. **Confirmed real, now fixed with a thread-local proxy + two-layer defense:**

1. `tools/_common.py`: installs `_ThreadLocalOutputProxy` on `sys.stdout` / `sys.stderr` once at import. `_suppress_output()` sets the current thread's target to `os.devnull` around every inline `envelope_call`.
2. `tasks/manager.py`: background worker calls `set_thread_output_target(task_log_fh)` while `func(task)` runs, then `reset_thread_output_target()` — only the worker thread's writes are redirected; other threads are unaffected.
3. `verbosity=0` on supported AutoGluon constructors/methods.

**Verified:** live stdio MCP test against `:full` showed ONLY valid JSON-RPC frames — no AutoGluon leakage, including with the thread-local proxy under concurrent worker threads.

**Thread-safety:** Output redirection is now thread-safe at `max_workers > 1` (thread-local targets, global proxy is read-only after install). Safe to raise `MCP_MAX_WORKERS` for parallel training.

## Code Review Results (latest session — v0.2.0 hardening round)

- **0 CRITICAL, 3 HIGH (all fixed), 1 MEDIUM (fixed), 0 LOW.**
- Verdict: **APPROVE.**
- HIGH fixed: path traversal in multimodal image-column values (added `_resolve_image_path()` confinement to `ARTIFACTS_DIR`).
- HIGH fixed: public tools leaking `ValueError` before `envelope_call` (added `safe_tool` decorator applied to every public tool + defense-in-depth wrapper in `server.py`).
- HIGH fixed: full Python tracebacks written to user-facing task logs (removed `traceback.format_exc()` from `tasks/manager.py`).
- MEDIUM fixed: LRU duplicate-load race in `_load_model` (resolved via `get_or_load()` with per-key lock + double-checked loading — see Known Limitations; the previous "benign" entry has been retired).

### Prior round (Phase 3 release)

- **0 CRITICAL, 1 HIGH (fixed), 1 MEDIUM (fixed), 1 MEDIUM (noted/benign → now resolved), 0 LOW.**
- HIGH fixed: registry lock `threading.Lock()` → `threading.RLock()` (sweep() re-enters the store lock — non-reentrant deadlocked).
- MEDIUM fixed: CANCELLED-before-execution branch now sets `finished_at`.
- MEDIUM noted/benign (now resolved): `_load_model` non-atomic check-then-set — resolved in the hardening round by `get_or_load()`.

## Resolved in v0.2.0 (Phase 3 tech-debt — 4 items DONE)

1. **Soft-cancel status race** (`tasks/registry.py`, `tasks/manager.py`) — per-task `_state_lock`; terminal states (SUCCESS/FAILED/CANCELLED) are sticky. A cancel arriving after completion returns `already_terminal` instead of overwriting.
2. **Unbounded predictor cache** (`tools/model_management.py`, `config.py`) — replaced the plain dict with the thread-safe `_ModelLRUCache` (OrderedDict, move-to-end, popitem(last=False)); new env `MCP_MODEL_CACHE_MAX` (default 4).
3. **TaskStore retention** (`tasks/registry.py`, `config.py`) — `sweep()` runs on `add`/`get`/`list`/`snapshot`/`require`; evicts terminal tasks older than `MCP_TASK_RETENTION_SECONDS` (default 86400) or over cap `MCP_TASK_MAX_RETAINED` (default 100); never evicts running/pending; evicted-id lookup raises a clear "Task expired or not found" (caught, not a crash).
4. **stdout-redirect thread-safety** (`tools/_common.py`, `tasks/manager.py`) — `_ThreadLocalOutputProxy` installed once as `sys.stdout`/`sys.stderr` at import; `_suppress_output()` and the background worker set thread-local targets instead of swapping the global stream → safe at `max_workers > 1`. New helpers `set_thread_output_target()` / `reset_thread_output_target()`.

## Known Limitations

- Training `fit()` can run for a long time; `cancel_task` is a **soft cancel** (cannot hard-kill a thread). Actual interruption relies on `time_limit` — always set a reasonable one.
- streamable-http mode is currently **unauthenticated** — trusted networks only.
- Windows-native Python execution is not supported.
- Progress parsing (live training-log tailing) is not implemented — poll `get_task_status` for `log_tail`.
- CI lint pipeline and 80% test-coverage target are not yet in place (optional).

## Hardening Round — 2026-07-09 (post-v0.2.0, e2e-runner + code-reviewer)

**Test counts after this round:** `:latest` **52 passed, 2 skipped**; `:full` **56 passed, 0 skipped, 0 failed**. +8 regression tests added.

### AutoGluon 1.5.0 API-drift fixes (e2e-runner)

1. `tools/tabular.py` `_evaluate_tabular` — was passing `metric=metrics[0]` to `TabularPredictor.evaluate()`; AutoGluon 1.5.0 `evaluate()` has no `metric` parameter and returns a dict of all metrics. **Fixed:** calls `evaluate(df)` once and filters the returned dict to the requested metric subset.
2. `tools/tabular.py` `feature_importance_tabular` — was passing `verbosity=0` to `TabularPredictor.feature_importance()`; AutoGluon 1.5.0 `feature_importance()` has no `verbosity` parameter and no `**kwargs`. **Fixed:** removed `verbosity=0` from both calls. This corrects the prior claim (item #5 above and checklist item #10) that `verbosity` was "kept on feature_importance".

### Security / correctness / error-handling fixes (code-reviewer)

3. **HIGH — path traversal** (`tools/multimodal.py`). Image-column values (`../`, absolute paths) could read outside `ARTIFACTS_DIR`. **Fixed:** added `_resolve_image_path()` helper — rejects absolute paths, resolves relative paths against `ARTIFACTS_DIR`, raises `ValueError` if the resolved path escapes the artifacts root.
4. **HIGH — exception leakage** (`tools/_common.py`, `server.py`, all `tools/*.py`). Public tools raised `ValueError` before `envelope_call`, bypassing the unified `{success, data, error}` envelope. **Fixed:** added `safe_tool` decorator in `tools/_common.py`, applied to every public tool. `server.py` also wraps registered tools with `safe_tool` as defense-in-depth. `functools.wraps` preserves FastMCP schemas.
5. **HIGH — traceback leakage** (`tasks/manager.py`). Full `traceback.format_exc()` was written to task logs, exposed via `get_task_status` / `log_tail`. **Fixed:** removed `traceback` import and the full traceback from the user-facing FAILED log line; only the exception message is retained.
6. **MEDIUM — LRU duplicate-load race** (`tools/model_management.py`). `_load_model` had a non-atomic check-then-set; under `MCP_MAX_WORKERS>1` two concurrent loads of the same uncached model were redundant. **Fixed:** added `get_or_load()` to `_ModelLRUCache` with a per-cache load lock + double-checked loading; `_load_model` now uses it. This resolves the "benign LRU duplicate-load race" previously listed under Known Limitations (entry removed).

## Additional Bugs Found + Fixed in `:full` Verification Pass

1. `tools/multimodal.py` — missing `from pathlib import Path` (used in image validation). Fixed.
2. `tools/multimodal.py` — `_VALID_PROBLEM_TYPES` too narrow; `"text_classification"` unsupported, `"classification"` literal caused failures. Expanded set; `"multimodal"` and `"classification"` map to `None` so AutoGluon infers. Fixed.
3. `tests/test_timeseries.py` — 3 rows/series was too few (TimeSeriesPredictor needs >=7, filtered to 0). Now 9 daily obs x 2 items. Fixed.
4. `tests/test_multimodal.py` — `"text_classification"` unsupported + only 4 samples -> "No model available". Changed to `problem_type="binary"`, 20 samples, `presets="medium_quality"`, `time_limit=60`. Fixed.
5. `tools/timeseries.py` / `multimodal.py` / `tabular.py` — `verbosity=0` passed to methods that don't accept it (leaderboard, predict, evaluate, MM fit). Removed from those; kept on constructors/fit/fit_summary. **Correction (hardening round):** `feature_importance()` in AutoGluon 1.5.0 also does NOT accept `verbosity` (no `**kwargs`); `verbosity=0` was removed from `feature_importance_tabular` too. Fixed.

## Verification Checklist for `:full` Image — ALL PASS/FIXED ✅

All 10 items verified against real AutoGluon 1.5.0 in `sy-automl-mcp:full`:

1. ✅ TS `freq` in constructor produces expected frequency inference.
2. ✅ TS no-dataset predict fallback correctly reloads training dataset from registry.
3. ✅ TS output shape after `reset_index()` includes item_id/timestamp.
4. ✅ TS evaluate with non-default id/time columns.
5. ✅ TS evaluate accepts metrics list.
6. ✅ **TS/Multimodal stdout pollution** — confirmed real, fixed with thread-local proxy + two-layer defense (see above). Verified: no leakage on stdio.
7. ✅ Multimodal `problem_type=None` auto-detection (via expanded `_VALID_PROBLEM_TYPES` + `None` mapping).
8. ✅ Multimodal image path validation (absolute + relative-to-ARTIFACTS_DIR). Fixed missing `pathlib` import. **Hardening round:** `_resolve_image_path()` now confines resolution to `ARTIFACTS_DIR` (path-traversal mitigation).
9. ✅ Multimodal `evaluate(metrics=list)` acceptance.
10. ✅ Unknown kwargs pattern — `verbosity=0` removed from methods that don't accept it; kept on supported constructors/fit/fit_summary. **Hardening-round correction:** `feature_importance()` (1.5.0) also does not accept `verbosity` — removed from `feature_importance_tabular` too.

## File Inventory

```
Root: server.py, config.py, Dockerfile, Dockerfile.test, docker-compose.yml,
      pyproject.toml, requirements.txt, requirements-full.txt, requirements-dev.txt,
      CLAUDE.md, PLAN.md, README.md, PROGRESS.md, .gitignore, .dockerignore,
      .python-version, .github/workflows/docker.yml, e2e_stdio.py

tools/: tabular.py, timeseries.py, multimodal.py, model_management.py,
        data.py, task_status.py, _common.py

tasks/: manager.py, registry.py

serialization/: envelope.py, dataframe.py

tests/: test_tabular.py, test_timeseries.py, test_multimodal.py,
        test_model_management.py, test_envelope.py, test_serialization.py,
        test_data.py, test_tasks.py, test_stdout_threading.py, conftest.py
```
