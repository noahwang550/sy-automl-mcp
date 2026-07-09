# sy-automl-mcp

> **v0.2.0** — Phase 1 + Phase 2 + Phase 3 all complete. 48 tests pass on `:full`, 46+2 skip on `:latest`. Live stdio MCP e2e verified.

将 [AutoGluon](https://github.com/autogluon/autogluon) 的 AutoML 能力封装为 **MCP (Model Context Protocol) 服务**，让 AI 助手（如 Claude Code）通过标准 MCP 工具调用完成数据加载、模型训练、预测、评估、模型管理全流程。

> **运行环境：Docker 优先。** AutoGluon 官方仅支持 Linux/macOS，原生 Windows 下多模态/torch 依赖不稳定。本项目通过 Linux 容器运行 MCP server，宿主为 Windows 时使用 Docker Desktop 即可，无需 WSL2 直装。

## What's New in v0.2.0

Phase 3 tech-debt — 4 hardening items resolved, all verified against real AutoGluon in `:full`:

1. **Per-task cancel race fixed** — `tasks/registry.py` + `tasks/manager.py` now use per-task `_state_lock` with sticky terminal states (SUCCESS/FAILED/CANCELLED). A cancel that arrives after completion returns `already_terminal` instead of overwriting the result.
2. **LRU model cache** — `tools/model_management.py` replaced its unbounded predictor dict with a thread-safe `_ModelLRUCache` (OrderedDict + move-to-end + popitem(last=False)). Cap is configurable via `MCP_MODEL_CACHE_MAX` (default `4`).
3. **Task retention** — `tasks/registry.py` runs `sweep()` on add/get/list/snapshot/require, evicting terminal tasks older than `MCP_TASK_RETENTION_SECONDS` (default `86400`) or over cap `MCP_TASK_MAX_RETAINED` (default `100`). Running/pending tasks are never evicted; evicted-id lookup raises a clear "Task expired or not found".
4. **Thread-safe stdout redirect** — `tools/_common.py` installs a process-wide `_ThreadLocalOutputProxy` on `sys.stdout`/`sys.stderr`. `_suppress_output()` and the background worker set thread-local targets instead of swapping the global stream. Now safe to raise `MCP_MAX_WORKERS` above `1` for parallel training.

Plus: registry lock upgraded to `RLock` (sweep() re-enters the store lock), CANCELLED-before-execution now sets `finished_at`, `_ThreadLocalOutputProxy` gained explicit `__iter__`/`__next__`, and a new live harness `e2e_stdio.py` drives a real stdio MCP round-trip via the `mcp` SDK.

## 当前状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1 — Tabular + stdio + 后台任务 | ✅ 已验证 | 端到端 stdio 流程 `load_dataset → train_tabular → get_task_status → predict_tabular` 经 e2e-runner 确认 |
| Phase 2 — TimeSeries / Multimodal / 模型管理 | ✅ 已验证 | 在 `:full` 镜像中对真实 AutoGluon 验证通过；10 项检查清单全部 PASS/FIXED |
| Phase 3 — 加固（错误信封、资源限制、LRU、保留策略、线程安全、CI） | ✅ 完成 | envelope ✅，资源限制 ✅，stdout 污染修复 ✅，线程安全 ✅，LRU 缓存 ✅，任务保留 ✅，取消竞争 ✅ |

**测试计数（v0.2.0）：** `:latest` **46 passed, 2 skipped**（TS/MM skip 符合预期，它们在 `:full` 中）；`:full` **48 passed, 0 skipped, 0 failed**（~2.5 min）。Live stdio MCP e2e：**PASSED**（24 个工具 + 干净 stdout）。

**关键事实：** 镜像 `sy-automl-mcp:latest`（tabular tier，autogluon.tabular 1.5.0 + pandas 2.3.3）和 `sy-automl-mcp:full`（+ timeseries + multimodal）均已构建并通过全部测试。MCP server stdio 启动正常，`tools/list` 返回 24 个工具。stdout 污染已通过线程本地代理 + 两层防御（`verbosity=0` + stdout/stderr 重定向）解决，`max_workers > 1` 安全。

## 快速开始

### 拉取预构建镜像（推荐）

```bash
# Tabular tier（默认，CPU 即可）
docker run -i --rm -v "$PWD/artifacts:/app/artifacts" ghcr.io/noahwang550/sy-automl-mcp:tabular

# Full tier（+ timeseries + multimodal，建议 GPU）
docker run --gpus all -i --rm -v "$PWD/artifacts:/app/artifacts" ghcr.io/noahwang550/sy-automl-mcp:full
```

> `v*` tag push 会自动触发 GHCR publish workflow（`.github/workflows/docker.yml`）。标签包括 `:latest`、`:tabular`、`:full`、`:v0.2.0`。

### 构建

```bash
# Tabular tier（默认，体积较小，CPU 即可）
docker build -t sy-automl-mcp .

# Full tier（+ timeseries + multimodal，建议 GPU）
docker build -t sy-automl-mcp:full --build-arg TIER=full .
```

### 运行

```bash
# stdio 模式（本地 Claude Code）
docker run -i --rm -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp

# streamable-http 模式（远程/共享）
docker run --rm -p 8000:8000 \
  -e MCP_TRANSPORT=http -e MCP_PORT=8000 \
  -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp
```

> **Windows Git Bash 注意：** 使用 `docker run -w /app` 时需要 `MSYS_NO_PATHCONV=1` 前缀，否则 Git Bash 会将 `/app` 自动转换为 Windows 路径。

### 在 Claude Code 中注册（stdio）

```bash
claude mcp add autogluon -- docker run -i --rm \
  -v /absolute/path/to/sy-automl-mcp/artifacts:/app/artifacts \
  sy-automl-mcp
```

## 安装 Tier

| Tier | 镜像标签 | 包含 | 用途 | 硬件 |
|------|----------|------|------|------|
| `tabular`（默认） | `sy-automl-mcp:latest` | `autogluon.tabular` | 表格分类/回归 | CPU 即可 |
| `full` | `sy-automl-mcp:full` | + `timeseries` + `multimodal` | 时序预测、图像/文本/多模态 | 建议 GPU |

```bash
# GPU 运行 full tier（多模态推荐）
docker run --gpus all -i --rm -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp:full
```

## 工具一览（24 个工具）

### 数据工具（2）

| 工具 | 说明 | Tier |
|------|------|------|
| `load_dataset` | 导入数据集（文件路径或内联 CSV），返回概要 | tabular |
| `validate_dataset` | 训练前数据预检（列、缺失、类型） | tabular |

### Tabular 工具（6）

| 工具 | 说明 | Tier |
|------|------|------|
| `train_tabular` | 后台训练 TabularPredictor，立即返回 `task_id` | tabular ✅ |
| `predict_tabular` | 用已训练模型预测（支持 dataset_id 或 inline_csv） | tabular ✅ |
| `leaderboard_tabular` | 返回模型排行榜 | tabular ✅ |
| `feature_importance_tabular` | 返回特征重要性 | tabular ✅ |
| `fit_summary_tabular` | 返回训练摘要 | tabular ✅ |
| `evaluate_tabular` | 评估模型，返回指标 | tabular ✅ |

### TimeSeries 工具（5）

| 工具 | 说明 | Tier |
|------|------|------|
| `train_timeseries` | 后台训练 TimeSeriesPredictor | full ✅ |
| `predict_timeseries` | 时序预测（无数据时回退到训练集） | full ✅ |
| `leaderboard_timeseries` | 时序模型排行榜 | full ✅ |
| `evaluate_timeseries` | 评估时序模型（支持自定义 id/time 列和指标） | full ✅ |
| `fit_summary_timeseries` | 时序训练摘要 | full ✅ |

### Multimodal 工具（3）

| 工具 | 说明 | Tier |
|------|------|------|
| `train_multimodal` | 后台训练 MultimodalPredictor（图像/文本/多模态） | full ✅ |
| `predict_multimodal` | 多模态预测（校验 image_path/text 列） | full ✅ |
| `evaluate_multimodal` | 评估多模态模型（支持 metrics 列表） | full ✅ |

### 模型管理工具（4）

| 工具 | 说明 | Tier |
|------|------|------|
| `list_models` | 列出所有已训练模型 | tabular |
| `load_model` | 预加载模型到内存缓存 | tabular |
| `model_info` | 查询单个模型详情 | tabular |
| `delete_model` | 删除模型（需 confirm=true） | tabular |

### 任务状态工具（4）

| 工具 | 说明 | Tier |
|------|------|------|
| `get_task_status` | 查询后台任务状态（pending/running/success/failed/cancelled） | tabular ✅ |
| `get_task_result` | 查询后台任务结果 | tabular ✅ |
| `cancel_task` | 软取消后台任务 | tabular ✅ |
| `list_tasks` | 列出所有后台任务 | tabular ✅ |

> ✅ = 已通过真实 AutoGluon 端到端验证（tabular 在 `:latest`，timeseries/multimodal 在 `:full`）

## 目录约定

- `artifacts/datasets/` — 导入的数据集
- `artifacts/models/<model_id>/` — AutoGluon 训练产物
- `artifacts/predictions/` — 预测输出
- `artifacts/logs/<task_id>.log` — 任务日志
- `artifacts/registry.json` — 模型注册表

`artifacts/` 以 volume 挂载，跨容器重建保留；已 gitignore。

## 开发与测试

```bash
# 运行测试（pytest 不在生产镜像中，需运行时安装）
docker run --rm --entrypoint sh \
  -v "$PWD/artifacts:/app/artifacts" \
  sy-automl-mcp \
  -c "pip install pytest pytest-asyncio -q && python -m pytest tests/ -v"

# 运行 lint
docker run --rm --entrypoint sh sy-automl-mcp \
  -c "pip install ruff -q && ruff check ."

# 或使用 docker compose（需要 compose 中配置 test profile）
docker compose run --rm app pytest
```

> **注意：** `pytest` 未打入生产镜像以减小体积。测试时需在容器内临时安装，或使用独立的测试镜像。
>
> **Windows Git Bash 注意：** `docker run` 命令中若使用 `-w /app` 等工作目录参数，需加 `MSYS_NO_PATHCONV=1` 前缀防止路径被自动转换。

## Stdout 污染防护

AutoGluon / PyTorch / Lightning 会向 stdout/stderr 输出进度条和横幅，可能破坏 MCP stdio JSON-RPC 流。本项目采用**线程本地代理 + 两层防御**：

1. **`tools/_common.py`** — 在 import 时一次性将 `sys.stdout`/`sys.stderr` 替换为进程级的 `_ThreadLocalOutputProxy`。`_suppress_output()` 上下文管理器将**当前线程**的目标设为 `os.devnull`，仅影响调用线程。
2. **`tasks/manager.py`** — 后台 worker 通过 `set_thread_output_target(task_log_fh)` 将**该 worker 线程**的输出重定向到任务日志文件；执行结束后调用 `reset_thread_output_target()`。其他线程不受影响。
3. 此外，支持 `verbosity` 参数的 AutoGluon 构造函数/方法均传入 `verbosity=0`。

已验证：`:full` 镜像的 stdio MCP 端到端测试（initialize → load_dataset → train_tabular → poll → predict_tabular）stdout 上仅有合法 JSON-RPC 帧，无 AutoGluon 泄漏，包括在并发 worker 线程下。

> **线程安全：** stdout/stderr 重定向现在是**线程安全的**（线程本地目标，代理在安装后只读）。可安全提高 `MCP_MAX_WORKERS` 以并行训练。

## 限制

- 训练 `fit()` 可能运行很久；`cancel_task` 为**软取消**（无法硬杀线程），实际中断依赖 `time_limit`，请始终为训练设置合理的 `time_limit`。
- streamable-http 模式当前**无认证**，仅限可信网络。
- Windows 原生 Python 运行不在支持范围。
- **良性的 LRU 重复加载竞争：** `tools/model_management.py` 中 `_load_model` 使用非原子性的检查-然后-设置，在 `max_workers > 1` 下两个并发调用可能同时加载同一个未缓存的模型（冗余工作，无崩溃，无正确性问题 —— 第二次加载简单覆盖第一次的条目）。在默认单 worker 配置下为良性。记录为未来加固项。
- 进度解析（实时训练日志 tailing）未实现 —— 通过 `get_task_status` 轮询 `log_tail`。
- CI lint pipeline 与 80% 测试覆盖率目标尚未到位（可选）。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MCP_TRANSPORT` | `stdio` | `stdio` 或 `http`（streamable-http） |
| `MCP_PORT` | `8000` | streamable-http 模式的监听端口 |
| `MCP_MAX_WORKERS` | `1` | 后台任务线程池大小（v0.2.0 起可安全提高） |
| `MCP_MODEL_CACHE_MAX` | `4` | 内存中预测器 LRU 缓存上限 |
| `MCP_TASK_RETENTION_SECONDS` | `86400` | 终态任务记录保留时长（秒） |
| `MCP_TASK_MAX_RETAINED` | `100` | 终态任务最大保留数量 |
| `MAX_DATASET_ROWS` / `MAX_DATASET_MB` / `MAX_DATASET_COLUMNS` | — | 数据集资源限制 |
