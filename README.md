# sy-automl-mcp

将 [AutoGluon](https://github.com/autogluon/autogluon) 的 AutoML 能力封装为 **MCP (Model Context Protocol) 服务**，让 AI 助手（如 Claude Code）通过标准 MCP 工具调用完成数据加载、模型训练、预测、评估、模型管理全流程。

> **运行环境：Docker 优先。** AutoGluon 官方仅支持 Linux/macOS，原生 Windows 下多模态/torch 依赖不稳定。本项目通过 Linux 容器运行 MCP server，宿主为 Windows 时使用 Docker Desktop 即可，无需 WSL2 直装。

## 当前状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1 — Tabular + stdio + 后台任务 | ✅ 已验证 | 31 测试通过，端到端 stdio 流程 `load_dataset → train_tabular → get_task_status → predict_tabular` 经 e2e-runner 确认 |
| Phase 2 — TimeSeries / Multimodal / 模型管理 | ✅ 已验证 | 在 `:full` 镜像中对真实 AutoGluon 验证通过；33 测试全部通过（0 skip），10 项检查清单全部 PASS/FIXED |
| Phase 3 — 加固（错误信封、资源限制、CI、覆盖率） | 🔶 部分完成 | 统一 envelope ✅，资源限制 ✅；进度解析 ❌，CI lint ❌，80% 覆盖率 ❌ |

**关键事实：** 镜像 `sy-automl-mcp:latest`（tabular tier，autogluon.tabular 1.5.0 + pandas 2.3.3）和 `sy-automl-mcp:full`（+ timeseries + multimodal）均已构建并通过全部测试。MCP server stdio 启动正常，`tools/list` 返回 24 个工具。stdout 污染已通过两层防御（`verbosity=0` + stdout/stderr 重定向）解决。Git 已 init 但**尚无 commit**。

## 快速开始

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

AutoGluon / PyTorch / Lightning 会向 stdout/stderr 输出进度条和横幅，可能破坏 MCP stdio JSON-RPC 流。本项目采用**两层防御**：

1. **`tools/_common.py`** — `_suppress_output()` 上下文管理器在每次内联 `envelope_call` 期间将 `sys.stdout`/`sys.stderr` 重定向到 `os.devnull`。
2. **`tasks/manager.py`** — 后台 worker 在 `func(task)` 执行期间将 `sys.stdout`/`sys.stderr` 重定向到任务日志文件。
3. 此外，支持 `verbosity` 参数的 AutoGluon 构造函数/方法均传入 `verbosity=0`。

已验证：`:full` 镜像的 stdio MCP 端到端测试（initialize → load_dataset → train_tabular → poll → predict_tabular）stdout 上仅有合法 JSON-RPC 帧，无 AutoGluon 泄漏。

> **线程安全限制：** 全局 `sys.stdout`/`sys.stderr` 重定向**不是线程安全的**。默认 `MCP_MAX_WORKERS=1`（串行训练）和顺序 stdio 请求处理下安全，但若 `max_workers > 1` 则需要 per-worker 重定向方案（如 per-thread `io.StringIO` 或基于 logging 的捕获）。

## 限制

- 训练 `fit()` 可能运行很久；`cancel_task` 为**软取消**（无法硬杀线程），实际中断依赖 `time_limit`，请始终为训练设置合理的 `time_limit`。
- streamable-http 模式当前**无认证**，仅限可信网络。
- Windows 原生 Python 运行不在支持范围。
- 全局 stdout/stderr 重定向不是线程安全的（见上方说明），`max_workers > 1` 时需要额外改造。
- 任务管理器为软取消，存在状态竞争（见技术债务）；预测器缓存无上限；已完成任务无 TTL 淘汰。

## 技术债务（已知，已记录）

1. **软取消状态竞争** — `tasks/manager.py` + `registry.py` 缺少 per-task 锁，cancel 与 status 更新存在竞争窗口
2. **预测器缓存无上限** — `tools/model_management.py` 中 `load_model` 缓存无 LRU 限制，长期运行可能耗尽内存
3. **已完成任务无淘汰** — TaskStore 不淘汰已完成任务记录，需增加 TTL/保留策略
4. **stdout 重定向非线程安全** — 全局 `sys.stdout`/`sys.stderr` 重定向在 `max_workers > 1` 时会互相干扰，需要 per-worker 捕获方案
