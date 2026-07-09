# Implementation Plan: sy-automl-mcp (AutoGluon MCP Server)

> 将 AutoGluon 的 AutoML 能力（Tabular / TimeSeries / Multimodal）封装为符合 Model Context Protocol 的工具服务，使 AI 助手（如 Claude Code）能通过标准 MCP 工具调用完成数据校验、模型训练、预测、评估、模型管理全流程。服务采用 stdio 优先传输、后台任务模式处理长耗时 fit/predict，并统一管理 artifacts 目录。

## 阶段状态总览

| 阶段 | 状态 | 说明 |
|------|------|------|
| **Phase 1** — Tabular + stdio + 后台任务 | ✅ 完成并验证 | 31 测试通过，端到端 stdio 流程经 e2e-runner 确认；镜像 `sy-automl-mcp:latest` 可运行 |
| **Phase 2** — TimeSeries / Multimodal / 模型管理 | ✅ 完成并验证 | `:full` 镜像中真实 AutoGluon 验证通过；33 测试全部通过（0 skip）；10 项检查清单全部 PASS/FIXED |
| **Phase 3** — 加固 | ✅ 完成（v0.2.0） | envelope ✅，资源限制 ✅，stdout 污染修复 ✅（线程本地代理），取消竞争 ✅，LRU 缓存 ✅，任务保留 ✅，线程安全 stdout ✅；可选项未做：进度解析、CI lint、80% 覆盖率 |

## 需求清单

- 暴露 TabularPredictor / TimeSeriesPredictor / MultimodalPredictor 三类核心能力为 MCP 工具。
- 长耗时训练后台执行，立即返回 task_id，支持状态/结果/取消查询。
- 数据集与模型 artifact 统一存放、可命名、可列出、可删除、可加载。
- AutoGluon 对象（DataFrame、leaderboard 等）降维为 JSON 可序列化结构。
- 独立虚拟环境隔离 AutoGluon + torch 重依赖。
- Windows 主机下明确推荐容器方案，规避原生兼容性问题。

## 假设与约束

- Python 3.11（已验证与 AutoGluon 1.5.0 兼容）。
- 默认本地 stdio 传输；streamable-http 作为可选远程模式。
- 单机单进程；并发任务通过任务管理器串行化（`max_workers=1` 默认）。
- 不在 MCP 工具内做认证/多租户；假定本地可信调用方。
- 不在代码仓库内提交数据集与模型产物（gitignore）。

## 架构总览

### 运行环境（Docker 优先）

| 选项 | 说明 | 推荐度 |
|------|------|--------|
| **Docker 容器（Linux 基础镜像）** | 基于 `python:3.11-slim`，分 tier 安装；隔离重依赖、规避 Windows 兼容性问题 | 强烈推荐（本项目默认） |
| Docker + docker-compose | 一键启动，artifacts 以 volume 挂载持久化 | 推荐 |
| WSL2 直接运行 | 开发调试备选 | 备选 |
| 原生 Windows Python | 多模态/torch vision 不稳定 | 不推荐 |

### 项目结构树

```
D:\claudecode\sy-automl-mcp\
├── Dockerfile                      # python:3.11-slim + AutoGluon（分 tier）
├── Dockerfile.test                 # 轻量测试镜像
├── docker-compose.yml              # 一键启动 + artifacts volume
├── .dockerignore
├── CLAUDE.md
├── PLAN.md
├── PROGRESS.md
├── README.md
├── pyproject.toml
├── requirements.txt                # tabular tier pin
├── requirements-full.txt           # full tier (+ timeseries + multimodal)
├── requirements-dev.txt            # ruff, pytest, pytest-asyncio
├── .gitignore
├── .python-version                 # 3.11
├── .github/workflows/docker.yml    # GHCR publish on v* tag
├── server.py                       # FastMCP 入口，注册 24 个工具
├── config.py                       # 路径常量、环境变量、registry 辅助
├── tools/
│   ├── _common.py                  # 工具间共享辅助
│   ├── tabular.py                  # TabularPredictor 工具（6 个）
│   ├── timeseries.py               # TimeSeriesPredictor 工具（5 个）
│   ├── multimodal.py               # MultimodalPredictor 工具（3 个）
│   ├── model_management.py         # list/load/info/delete 模型（4 个）
│   ├── data.py                     # load_dataset / validate_dataset（2 个）
│   └── task_status.py              # get_task_status/result/cancel/list（4 个）
├── tasks/
│   ├── manager.py                  # TaskManager：ThreadPoolExecutor + 软取消
│   └── registry.py                 # task_id -> Task 记录表
├── serialization/
│   ├── envelope.py                 # 统一 {success, data, error} 返回格式
│   └── dataframe.py                # DataFrame -> dict/list 降维
├── artifacts/                       # 运行时目录（gitignore）
│   ├── datasets/
│   ├── models/
│   ├── predictions/
│   ├── logs/
│   └── registry.json
└── tests/
    ├── conftest.py
    ├── test_tabular.py
    ├── test_timeseries.py
    ├── test_multimodal.py
    ├── test_model_management.py
    ├── test_tasks.py
    ├── test_data.py
    ├── test_serialization.py
    └── test_envelope.py
```

### 后台任务管理器设计

- **线程 + threading.Lock**（非 asyncio）：AutoGluon API 为同步阻塞调用，线程最简单且可控。
- `TaskManager` 单例：`submit → task_id`，`ThreadPoolExecutor(max_workers=1)` 默认串行。
- `Task` 字段：`task_id, type, status, created_at, started_at, finished_at, result_summary, error, artifact_path, params`。
- 软取消：设置标志，实际中断依赖 `time_limit`。
- 日志：每任务 `artifacts/logs/<task_id>.log`，`get_task_status` 返回尾部 N 行。

### Artifacts 目录约定

- 数据集：`artifacts/datasets/<dataset_id>/`
- 模型：`artifacts/models/<model_id>/`（AutoGluon `path` 参数）
- 预测：`artifacts/predictions/<prediction_id>.json`
- 注册表：`artifacts/registry.json`

## 核心工具接口设计（24 个工具）

### 数据工具（2）

| 工具名 | 参数 | 返回值 | 说明 |
|--------|------|--------|------|
| `load_dataset` | `source, dataset_id, format?` | `{dataset_id, path, rows, columns, dtypes, sample}` | 导入数据到 artifacts/datasets |
| `validate_dataset` | `dataset_id, task_type?, target?, required_columns?` | `{valid, issues, inferred_dtypes, missing_counts}` | 训练前预检 |

### Tabular 工具（6）✅ 已验证

| 工具名 | 参数 | 返回值 | 说明 |
|--------|------|--------|------|
| `train_tabular` | `dataset_id, target, model_id, problem_type?, eval_metric?, time_limit?, presets?, hyperparameters?, random_seed?` | `{task_id, model_id}` | 后台 TabularPredictor.fit |
| `predict_tabular` | `model_id, dataset_id?/inline_csv?, prediction_id?` | `{predictions: [...]}` 或 `{task_id}` | 预测（小数据内联，大数据后台） |
| `leaderboard_tabular` | `model_id, extra_info?` | `{leaderboard: [...], best_model}` | 排行榜 |
| `feature_importance_tabular` | `model_id, dataset_id?` | `{importances: [...]}` | 特征重要性 |
| `fit_summary_tabular` | `model_id` | `{summary, best_model, model_types}` | 训练摘要 |
| `evaluate_tabular` | `model_id, dataset_id, metrics?` | `{metrics: {name: value}}` | 评估（取首个指标） |

### TimeSeries 工具（5）✅ 已验证

| 工具名 | 参数 | 返回值 | 说明 |
|--------|------|--------|------|
| `train_timeseries` | `dataset_id, target, model_id, prediction_length, freq?, time_column, id_column?, eval_metric?, time_limit?, presets?` | `{task_id, model_id}` | TimeSeriesPredictor.fit（freq 在构造函数中） |
| `predict_timeseries` | `model_id, data?, prediction_length?` | `{predictions: [...]}` | 无数据时回退训练集 |
| `leaderboard_timeseries` | `model_id` | `{leaderboard: [...]}` | 排行榜 |
| `evaluate_timeseries` | `model_id, dataset_id, metrics?, id_column?, time_column?` | `{metrics: {...}}` | 支持自定义列和指标 |
| `fit_summary_timeseries` | `model_id` | `{summary}` | 训练摘要 |

### Multimodal 工具（3）✅ 已验证

| 工具名 | 参数 | 返回值 | 说明 |
|--------|------|--------|------|
| `train_multimodal` | `dataset_id, model_id, problem_type?, label?, image_path_column?, text_column?, time_limit?, presets?` | `{task_id, model_id}` | 校验 image/text 列 |
| `predict_multimodal` | `model_id, data, prediction_id?` | `{predictions: [...]}` | |
| `evaluate_multimodal` | `model_id, dataset_id, metrics?` | `{metrics: {...}}` | 转发 metrics 列表 |

### 模型管理工具（4）

| 工具名 | 参数 | 返回值 | 说明 |
|--------|------|--------|------|
| `list_models` | `filter?` | `{models: [...]}` | 读 registry.json |
| `load_model` | `model_id` | `{model_id, status: "loaded"}` | 预加载到内存缓存 |
| `model_info` | `model_id` | `{model_id, info: {...}}` | 查询详情 |
| `delete_model` | `model_id, confirm` | `{deleted: true, freed_mb}` | 删目录 + 更新 registry |

### 任务状态工具（4）✅ 已验证

| 工具名 | 参数 | 返回值 | 说明 |
|--------|------|--------|------|
| `get_task_status` | `task_id` | `{task_id, status, type, elapsed_sec, log_tail}` | 状态轮询 |
| `get_task_result` | `task_id` | `{task_id, status, result_summary, artifact_path}` | 结果查询 |
| `cancel_task` | `task_id` | `{task_id, status: "cancellation_requested"}` | 软取消 |
| `list_tasks` | 无 | `{tasks: [...]}` | 列出所有任务 |

## 实施步骤

### Phase 1: MVP — Tabular + stdio + 后台任务最小闭环 ✅

目标：从 Claude Code 调用 `train_tabular` → 轮询 `get_task_status` → `get_task_result` → `predict_tabular` 跑通一个 CSV 分类任务。

1. ~~**仓库初始化**~~ ✅ — git init, .gitignore, pyproject.toml, requirements.txt
2. ~~**环境与文档**~~ ✅ — README.md, Docker 化
3. ~~**配置模块**~~ ✅ — config.py
4. ~~**序列化模块**~~ ✅ — serialization/dataframe.py
5. ~~**任务管理器**~~ ✅ — tasks/manager.py, tasks/registry.py
6. ~~**数据工具**~~ ✅ — tools/data.py
7. ~~**Tabular 工具**~~ ✅ — tools/tabular.py（6 个工具）
8. ~~**任务状态工具**~~ ✅ — tools/task_status.py
9. ~~**FastMCP 入口**~~ ✅ — server.py
10. ~~**冒烟测试**~~ ✅ — 31 passed, 2 skipped

**交付物：** ✅ 可启动的 stdio MCP server，Tabular 训练/预测/leaderboard + 任务管理闭环 + 数据加载；31 测试通过；端到端验证通过。

### Phase 2: 完整能力 — TimeSeries / Multimodal / 模型管理 ✅

目标：覆盖三类 Predictor 全部工具，模型可被列出/加载/删除。

11. ~~**模型管理工具**~~ ✅ — tools/model_management.py（list/load/info/delete）
12. ~~**TimeSeries 工具**~~ ✅ — tools/timeseries.py（5 个工具），`:full` 镜像验证通过
13. ~~**Multimodal 工具**~~ ✅ — tools/multimodal.py（3 个工具），`:full` 镜像验证通过
14. ~~**registry 增强**~~ ✅ — config.py 中 register_model/remove_model
15. ~~**streamable-http 传输**~~ ✅ — server.py 支持 MCP_TRANSPORT=http
16. ~~**集成测试**~~ ✅ — test_timeseries.py, test_multimodal.py, test_model_management.py 全部通过

**代码审查结果：** 0 CRITICAL, 7 HIGH（全部已修复）, 3 MEDIUM（延期至 Phase 3）, 2 LOW（已记录）。

**7 个 HIGH 修复：**
1. `freq` 从 `TimeSeriesPredictor.fit()` 移至构造函数
2. `predict_timeseries` 无数据集时通过 registry 回退加载训练数据
3. `reset_index()` 确保时序预测输出包含 item_id/timestamp
4. `evaluate_timeseries` 正确使用 id_column/time_column + metrics
5. `train_multimodal` 校验 image_path_column/text_column 及图像文件存在性
6. `evaluate_multimodal` 正确转发 metrics 列表
7. `predict_tabular` 强制 dataset_id/inline_csv 二选一；`evaluate_tabular` 取首个指标

**`:full` 验证额外修复（5 个 bug）：**
1. `tools/multimodal.py` — 缺少 `from pathlib import Path`（图像校验中使用）。已修复。
2. `tools/multimodal.py` — `_VALID_PROBLEM_TYPES` 过窄；`"text_classification"` 不支持，`"classification"` 字面量导致失败。扩展集合；`"multimodal"` 和 `"classification"` 映射到 `None` 让 AutoGluon 推断。已修复。
3. `tests/test_timeseries.py` — 3 行/系列太少（TimeSeriesPredictor 需 >=7，被过滤为 0）。改为 9 个日观测 x 2 个 item。已修复。
4. `tests/test_multimodal.py` — `"text_classification"` 不支持 + 仅 4 个样本导致 "No model available"。改为 `problem_type="binary"`，20 样本，`presets="medium_quality"`，`time_limit=60`。已修复。
5. `tools/timeseries.py` / `multimodal.py` / `tabular.py` — `verbosity=0` 传给不支持它的 API（leaderboard, predict, evaluate, MM fit）。从这些方法中移除；保留在构造函数/fit/fit_summary/feature_importance。已修复。

**状态：** ✅ 全部完成，`sy-automl-mcp:full` 镜像中真实 AutoGluon 验证通过。33 测试通过，0 skip，0 失败。

### Phase 3: 加固 — 错误处理、并发、文档、测试 ✅（v0.2.0）

17. ~~**错误处理统一**~~ ✅ — `serialization/envelope.py` 统一 `{success, data, error}` 返回格式
18. ~~**并发与资源控制**~~ ✅ — `MCP_MAX_WORKERS` 配置，`MAX_DATASET_ROWS/MB/COLUMNS` 资源限制
19. **进度查询增强** ❌（可选） — 训练日志解析未实现；当前通过 `get_task_status` 轮询 `log_tail`
20. **工具参考文档** ❌（可选） — docs/tools_reference.md 未创建
21. **架构文档** ❌（可选） — docs/architecture.md 未创建
22. **测试覆盖率** ❌（可选） — 目标 80% 未达标
23. **CI 与 lint** ❌（可选） — 仅有 docker.yml（GHCR publish），缺 CI lint pipeline
24. ~~**Stdout 污染修复**~~ ✅ — 线程本地代理（`_ThreadLocalOutputProxy`）+ 两层防御（`_suppress_output()` + 后台 worker 重定向 + `verbosity=0`），已验证 stdio 无泄漏，并发 worker 安全
25. ~~**取消竞争修复**~~ ✅（v0.2.0）— per-task `_state_lock`，终态粘性（SUCCESS/FAILED/CANCELLED），`already_terminal` 返回；registry 锁升级为 `RLock`
26. ~~**LRU 预测器缓存**~~ ✅（v0.2.0）— `_ModelLRUCache`（OrderedDict），`MCP_MODEL_CACHE_MAX`（默认 4）
27. ~~**任务保留策略**~~ ✅（v0.2.0）— `sweep()` 淘汰终态任务（`MCP_TASK_RETENTION_SECONDS`、`MCP_TASK_MAX_RETAINED`），永不淘汰运行中/等待中任务；过期 ID 查找抛出清晰异常
28. ~~**线程安全 stdout 重定向**~~ ✅（v0.2.0）— `_ThreadLocalOutputProxy` + `set_thread_output_target()` / `reset_thread_output_target()`；`max_workers > 1` 安全
29. ~~**Live stdio e2e 测试**~~ ✅（v0.2.0）— `e2e_stdio.py`（mcp SDK，24 tools + 干净 stdout + tabular 完整往返）

**交付物：** envelope ✅，资源限制 ✅，stdout 污染修复（线程安全）✅，取消竞争 ✅，LRU 缓存 ✅，任务保留 ✅，线程安全 stdout ✅，live e2e harness ✅。48 tests passed on `:full`，46+2 on `:latest`。Live stdio MCP e2e PASSED。

## 设计决策记录

### 本会话决策

- **统一 envelope 设计：** 所有工具返回 `{success: bool, data: Any, error: str | None}` 格式，通过 `serialization/envelope.py` 实现。
- **7 个 HIGH bug 修复：** 见 Phase 2 详述。
- **3 个 MEDIUM 延期：** 软取消状态竞争（需 per-task lock）、预测器缓存无上限（需 LRU）、TaskStore 无淘汰（需 TTL）。记录为技术债务，Phase 3 处理。
- **4 个技术债务项：** 上述 3 个 MEDIUM + stdout 重定向非线程安全（`max_workers > 1` 时需要 per-worker 捕获）。**全部已在 v0.2.0 解决。**
- **v0.2.0 决策：** per-task `_state_lock` + 终态粘性解决取消竞争；`_ModelLRUCache`（OrderedDict）解决预测器缓存无上限；`sweep()` + TTL/count 解决任务无淘汰；`_ThreadLocalOutputProxy` 解决 stdout 线程安全；registry 锁升级为 `RLock` 解决 sweep 重入死锁。
- **良性 LRU 重复加载竞争：** `_load_model` 非原子检查-然后-设置在 `max_workers > 1` 下可能导致同一模型被重复加载（冗余工作，无崩溃）。记录为未来加固项，不阻塞 v0.2.0。
- **pytest 不入生产镜像：** 减小镜像体积，测试时运行时安装。
- **freq 参数位置：** 从 `fit()` 移至 `TimeSeriesPredictor` 构造函数，与 AutoGluon 1.5.0 API 对齐。
- **stdout 两层防御：** `_suppress_output()` 上下文管理器 + 后台 worker stdout/stderr 重定向到任务日志文件 + `verbosity=0`。已验证有效，但非线程安全。
- **Multimodal problem_type 映射：** `"multimodal"` 和 `"classification"` 映射到 `None`，让 AutoGluon 自动推断具体类型。

### 架构决策

- **线程而非 asyncio**：AutoGluon 全同步 API，asyncio 需 `run_in_executor` 包裹，徒增复杂度。
- **默认串行训练**：AutoGluon 单任务已占满资源；多任务并行易 OOM。
- **registry.json 而非数据库**：模型数量有限，JSON 文件足够。
- **软取消**：Python 无法安全硬杀线程；靠 `time_limit` + 软标志。
- **stdio 优先**：本地 Claude Code 场景最常见，零网络配置。
- **数据双入口**：`dataset_id`（已加载）或内联 CSV。

## 风险清单与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| Windows 原生不支持 AutoGluon 多模态 | High | Docker 容器化，README 首段警告 |
| `fit()` 运行数小时，无法硬中断 | High | 软取消 + 强制 `time_limit` 默认值 |
| AutoGluon + torch 安装体积大（数 GB） | Medium | 分 tier 安装，Docker 隔离 |
| **stdout 污染 MCP 协议** | High（已修复） | 线程本地代理（`_ThreadLocalOutputProxy`）+ 两层防御（`_suppress_output()` + 后台 worker 重定向 + `verbosity=0`）。已验证 stdio 无泄漏。v0.2.0 起并发 worker 也安全 |
| DataFrame 序列化失败（NaN/Timestamp/np） | Medium | `serialization/dataframe.py` 统一降维 |
| 并发训练耗尽资源 | Medium | `max_workers=1` 默认，LRU 缓存上限（v0.2.0 已实现 `MCP_MODEL_CACHE_MAX`） |
| 路径越界 | Medium | `config.validate_id()` 拒绝非法字符 |
| streamable-http 无认证 | Medium | 文档标注仅可信网络 |

## 成功标准

- [x] Docker 容器启动 stdio MCP server，Claude Code 能 list tools 并调用。（24 个工具已注册）
- [x] Phase 1：Tabular CSV 分类端到端跑通。（31 测试通过 + e2e 验证）
- [x] Phase 2：TimeSeries 与 Multimodal 训练/预测端到端跑通。（`:full` 镜像验证通过，33 passed 0 skipped）
- [x] 长耗时训练不阻塞 MCP 工具调用，立即返回 task_id。
- [x] 所有返回值 JSON 可序列化。（envelope + dataframe 降维）
- [x] artifacts/ 全部 gitignore，registry.json 一致。
- [ ] 测试覆盖率 >= 80%。（未达标）
- [ ] CI（ruff + pytest）通过。（CI lint pipeline 未创建）
