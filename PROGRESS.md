# PROGRESS.md — sy-automl-mcp 开发进度

> 最后更新：2026-07-09（v0.2.0）

## 当前状态

**Phase 1、Phase 2、Phase 3 全部完成并验证。v0.2.0 已发布。**

- `:latest` 镜像（tabular）：**46 passed, 2 skipped**（TS/MM skip 符合预期，它们在 `:full` 中）
- `:full` 镜像（tabular + timeseries + multimodal）：**48 passed, 0 skipped, 0 failed**（~2.5 min）
- 所有 24 个 MCP 工具在真实 AutoGluon 1.5.0 上验证通过
- stdout 污染已修复（线程本地代理 + 两层防御，已验证 stdio 无泄漏，并发 worker 安全）
- 10 项 `:full` 检查清单全部 PASS/FIXED
- Phase 3 四项技术债务全部解决（取消竞争、LRU 缓存、任务保留、线程安全 stdout）
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
5. `tools/timeseries.py` / `multimodal.py` / `tabular.py` — `verbosity=0` 传给不支持的 API。从 leaderboard/predict/evaluate/MM fit 中移除；保留在构造函数/fit/fit_summary/feature_importance。已修复。

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

## 已知良性限制

- **LRU 重复加载竞争：** `_load_model` 使用非原子检查-然后-设置，在 `max_workers > 1` 下两个并发调用可能都加载同一个未缓存模型（冗余工作，无崩溃）。在默认单 worker 配置下为良性。记录为未来加固项。

## Git 状态

- `6134717` feat: AutoGluon MCP server — tabular/timeseries/multimodal tools, Docker-first, background task manager, stdout-pollution guard, 33 tests passing（初始 commit）
- `v0.2.0` tag 已打并推送
- Remote `origin` = `https://github.com/noahwang550/sy-automl-mcp.git`（HTTPS + GCM）
- GHCR publish workflow 在 `v*` tag 时触发

## 下一步

**无验证缺口。** 剩余可选工作：
1. （可选）CI lint pipeline
2. （可选）80% 测试覆盖率
3. （可选）进度解析增强（实时训练日志 tailing）
4. （可选）LRU 重复加载竞争的原子化加固（仅在 `max_workers > 1` 成为常见场景时）

## 环境备忘

- Windows 11 宿主，无原生 Python；所有执行通过 Docker
- Git Bash 需 `MSYS_NO_PATHCONV=1` 防止路径转换
- 镜像 ENTRYPOINT 是 `python server.py`；运行 pytest 需 `--entrypoint sh`
- pytest 不在生产镜像中，需运行时安装
- `:full` 镜像构建耗时 20-40+ min（CUDA/torch 下载，网络依赖）
