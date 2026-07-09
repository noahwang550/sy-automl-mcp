# PROGRESS.md — sy-automl-mcp 开发进度

> 最后更新：2026-07-09（v0.2.0 + hardening round）

## 当前状态

**Phase 1、Phase 2、Phase 3 全部完成并验证。v0.2.0 已发布。Hardening round（2026-07-09）已完成。**

- `:latest` 镜像（tabular）：**52 passed, 2 skipped**（TS/MM skip 符合预期，它们在 `:full` 中）
- `:full` 镜像（tabular + timeseries + multimodal）：**56 passed, 0 skipped, 0 failed**（~2.5 min）
- 所有 24 个 MCP 工具在真实 AutoGluon 1.5.0 上验证通过
- stdout 污染已修复（线程本地代理 + 两层防御，已验证 stdio 无泄漏，并发 worker 安全）
- 10 项 `:full` 检查清单全部 PASS/FIXED
- Phase 3 四项技术债务全部解决（取消竞争、LRU 缓存、任务保留、线程安全 stdout）
- Hardening round 6 项修复全部完成（2 API 漂移 + 路径穿越 + 异常泄漏 + 回溯泄漏 + LRU 竞争）
- Live stdio MCP e2e 在 `:full` 中 **PASSED**（24 tools + 干净 stdout）

## Phase 1 — Tabular + stdio + 后台任务 ✅

- 镜像 `sy-automl-mcp:latest`（tabular tier，autogluon.tabular 1.5.0 + pandas 2.3.3）构建成功并可运行
- MCP server stdio 启动正常，`tools/list` 返回 24 个工具
- 端到端流程经 e2e-runner 验证：`load_dataset` (inline CSV) → `train_tabular` (returns task_id) → poll `get_task_status` until `success` → `predict_tabular` (returns predictions)
- 统一错误信封 `{success, data, error}` 已实现（`serialization/envelope.py`）
- 资源限制 `MAX_DATASET_ROWS/MB/COLUMNS` 已实现

## Phase 2 — TimeSeries / Multimodal / 模型管理 ✅

**在 `sy-automl-mcp:full` 镜像中对真实 AutoGluon 1.5.0 验证通过。**

- 完整测试套件：33 passed, 0 skipped, 0 failed（~2 min）— Phase 2 验证时计数；v0.2.0 已提升至 48 passed。
- 定向运行也全部通过：test_timeseries.py (1), test_multimodal.py (1), test_model_management.py (6)
- 代码审查结果：0 CRITICAL, 7 HIGH（全部修复）, 3 MEDIUM（延期）, 2 LOW（记录）

### `:full` 验证阶段额外修复的 5 个 bug

1. `tools/multimodal.py` — 缺少 `from pathlib import Path`（图像校验中使用）。已修复。
2. `tools/multimodal.py` — `_VALID_PROBLEM_TYPES` 过窄；`"text_classification"` 不支持，`"classification"` 字面量失败。扩展集合 + `None` 映射。已修复。
3. `tests/test_timeseries.py` — 3 行/系列太少（TimeSeriesPredictor 需 >=7，过滤为 0）。改为 9 日观测 x 2 item。已修复。
4. `tests/test_multimodal.py` — `"text_classification"` 不支持 + 仅 4 样本 -> "No model available"。改为 `binary`, 20 样本, `presets="medium_quality"`, `time_limit=60`。已修复。
5. `tools/timeseries.py` / `multimodal.py` / `tabular.py` — `verbosity=0` 传给不支持的 API。从 leaderboard/predict/evaluate/MM fit 中移除；保留在构造函数/fit/fit_summary。**修正（hardening round）：** AutoGluon 1.5.0 的 `feature_importance()` 同样不接受 `verbosity`（无 `**kwargs`），已从 `feature_importance_tabular` 中移除。已修复。

### Stdout 污染修复（重要）

**问题确认：** AutoGluon/PyTorch/Lightning 确实向 stdout/stderr 输出进度条和横幅，会破坏 MCP stdio JSON-RPC 流。

**两层防御（已实现并验证）：**
1. `tools/_common.py` — `_suppress_output()` 上下文管理器在每次内联 `envelope_call` 期间将 `sys.stdout`/`sys.stderr` 重定向到 `os.devnull`
2. `tasks/manager.py` — 后台 worker 在 `func(task)` 执行期间将 `sys.stdout`/`sys.stderr` 重定向到任务日志文件
3. 此外，支持 `verbosity` 参数的 AutoGluon API 均传入 `verbosity=0`

**验证结果：** `:full` 镜像的 stdio MCP 端到端测试（initialize → load_dataset → train_tabular → poll → predict_tabular）stdout 上仅有合法 JSON-RPC 帧，无 AutoGluon 泄漏。

### 10 项 `:full` 检查清单 — 全部 PASS/FIXED ✅

1. ✅ TS `freq` 在构造函数中产生预期的频率推断
2. ✅ TS 无数据集 predict 回退正确从 registry 重载训练数据
3. ✅ TS `reset_index()` 后输出包含 item_id/timestamp
4. ✅ TS evaluate 使用非默认 id/time 列
5. ✅ TS evaluate 接受 metrics 列表
6. ✅ TS/Multimodal stdout 污染 — 确认存在，已通过两层防御修复
7. ✅ Multimodal `problem_type=None` 自动检测（通过扩展的 `_VALID_PROBLEM_TYPES` + `None` 映射）
8. ✅ Multimodal 图像路径校验（修复了缺失的 `pathlib` 导入）
9. ✅ Multimodal `evaluate(metrics=list)` 接受
10. ✅ 未知 kwargs 模式 — `verbosity=0` 从不支持的 API 中移除

## Phase 3 — 加固 ✅ 完成

- ✅ 统一 envelope（`serialization/envelope.py`）
- ✅ 资源限制（`config.py`）
- ✅ GHCR publish workflow（`.github/workflows/docker.yml`，`v*` tag 触发）
- ✅ Stdout 污染修复（线程本地代理 + 两层防御）
- ✅ **取消竞争修复**（v0.2.0）— per-task `_state_lock` + 终态粘性（SUCCESS/FAILED/CANCELLED）；终态后取消返回 `already_terminal`
- ✅ **LRU 预测器缓存**（v0.2.0）— `_ModelLRUCache`（OrderedDict），`MCP_MODEL_CACHE_MAX`（默认 4）
- ✅ **任务保留策略**（v0.2.0）— `sweep()` 淘汰终态任务（`MCP_TASK_RETENTION_SECONDS`、`MCP_TASK_MAX_RETAINED`），永不淘汰运行中/等待中任务
- ✅ **线程安全 stdout 重定向**（v0.2.0）— `_ThreadLocalOutputProxy` + `set_thread_output_target()` / `reset_thread_output_target()`；`max_workers > 1` 安全
- ❌ 进度解析增强（可选）
- ❌ CI lint pipeline（可选）
- ❌ 80% 测试覆盖率（可选）

## v0.2.0 额外修复（审查/验证期间发现）

- `tasks/registry.py`：`_lock` 从 `threading.Lock()` 升级为 `threading.RLock()`（`sweep()` 重入 store 操作，非重入锁死锁）
- `tasks/manager.py`：CANCELLED-before-execution 分支现在也设置 `finished_at`（终态任务必须有完成时间戳）
- `tools/_common.py`：代理新增显式 `__iter__`/`__next__`（特殊方法在 type 上查找，而非通过 `__getattr__`）
- `e2e_stdio.py`：仓库根目录新增实时 stdio MCP 往返测试工具（通过 mcp SDK，断言 24 工具 + 干净 stdout）
- 新增 15 个测试：`tests/test_tasks.py` (+8)、`tests/test_model_management.py` (+3)、`tests/test_stdout_threading.py`（新文件，+4）

## 已知限制

- 训练 `fit()` 可能运行很久；`cancel_task` 为软取消，实际中断依赖 `time_limit`
- streamable-http 模式当前无认证
- Windows 原生 Python 不在支持范围
- 进度解析（实时训练日志 tailing）未实现
- CI lint pipeline 与 80% 测试覆盖率目标尚未到位（可选）

## Git 状态

- `6134717` feat: AutoGluon MCP server — tabular/timeseries/multimodal tools, Docker-first, background task manager, stdout-pollution guard, 33 tests passing（初始 commit）
- `v0.2.0` tag 已打并推送
- Remote `origin` = `https://github.com/noahwang550/sy-automl-mcp.git`（HTTPS + GCM）
- GHCR publish workflow 在 `v*` tag 时触发

## Hardening Round — 2026-07-09（e2e-runner + code-reviewer）

**测试计数：** `:latest` **52 passed, 2 skipped**；`:full` **56 passed, 0 skipped, 0 failed**。新增 8 个回归测试。

### AutoGluon 1.5.0 API 漂移修复（e2e-runner）

1. `tools/tabular.py` `_evaluate_tabular` — 原来向 `TabularPredictor.evaluate()` 传入 `metric=metrics[0]`；AutoGluon 1.5.0 的 `evaluate()` 无 `metric` 参数，返回全部指标字典。**已修复：** 调用 `evaluate(df)` 一次，过滤返回字典到请求的指标子集。
2. `tools/tabular.py` `feature_importance_tabular` — 原来向 `TabularPredictor.feature_importance()` 传入 `verbosity=0`；AutoGluon 1.5.0 的 `feature_importance()` 无 `verbosity` 参数且无 `**kwargs`。**已修复：** 从两处调用中移除 `verbosity=0`。

### 安全 / 正确性 / 错误处理修复（code-reviewer）

3. **HIGH — 路径穿越**（`tools/multimodal.py`）。图像列值（`../`、绝对路径）可读取 `ARTIFACTS_DIR` 之外的文件。**已修复：** 新增 `_resolve_image_path()` 辅助函数 — 拒绝绝对路径，相对于 `ARTIFACTS_DIR` 解析，若解析后路径逃逸则抛出 `ValueError`。
4. **HIGH — 异常泄漏**（`tools/_common.py`、`server.py`、所有 `tools/*.py`）。公开工具在 `envelope_call` 之前抛出 `ValueError`，绕过了统一的 `{success, data, error}` 信封。**已修复：** 在 `tools/_common.py` 中新增 `safe_tool` 装饰器，应用于所有公开工具。`server.py` 也以防御纵深方式对注册工具包装 `safe_tool`。`functools.wraps` 保留 FastMCP schema。
5. **HIGH — 回溯泄漏**（`tasks/manager.py`）。完整的 `traceback.format_exc()` 写入任务日志，通过 `get_task_status` / `log_tail` 暴露。**已修复：** 移除 `traceback` 导入，用户可见的 FAILED 日志行仅保留异常消息。
6. **MEDIUM — LRU 重复加载竞争**（`tools/model_management.py`）。`_load_model` 使用非原子检查-然后-设置；在 `MCP_MAX_WORKERS>1` 下两个并发加载同一未缓存模型是冗余的。**已修复：** 在 `_ModelLRUCache` 中新增 `get_or_load()`，使用 per-cache 加载锁 + 双重检查加载；`_load_model` 现在使用它。已解决的"良性 LRU 重复加载竞争"从已知限制中移除。

### 新增测试（+8）

- `tests/test_tabular.py` (+6): `test_feature_importance_tabular_regression`、`test_predict_requires_exactly_one_data_source`、`test_train_with_bad_target`、`test_evaluate_unsupported_metric_returns_empty`、`test_fit_summary_after_train`、`test_concurrent_training_keeps_stdout_clean`
- `tests/test_timeseries.py` (+1): `test_evaluate_timeseries_with_metrics_list`
- `tests/test_multimodal.py` (+1): `test_train_multimodal_rejects_unknown_problem_type`

## 下一步

**无验证缺口。** 剩余可选工作：
1. （可选）CI lint pipeline
2. （可选）80% 测试覆盖率
3. （可选）进度解析增强（实时训练日志 tailing）
4. （可选）streamable-http 认证

## 环境备忘

- Windows 11 宿主，无原生 Python；所有执行通过 Docker
- Git Bash 需 `MSYS_NO_PATHCONV=1` 防止路径转换
- 镜像 ENTRYPOINT 是 `python server.py`；运行 pytest 需 `--entrypoint sh`
- pytest 不在生产镜像中，需运行时安装
- `:full` 镜像构建耗时 20-40+ min（CUDA/torch 下载，网络依赖）
