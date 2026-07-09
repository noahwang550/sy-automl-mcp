# PROGRESS.md — sy-automl-mcp 开发进度

> 最后更新：2026-07-09

## 当前状态

**Phase 1 和 Phase 2 均完成并验证。Phase 3 部分完成。无验证缺口。**

- `:latest` 镜像（tabular）：31 passed, 2 skipped（TS/MM skip 符合预期，它们在 `:full` 中）
- `:full` 镜像（tabular + timeseries + multimodal）：**33 passed, 0 skipped, 0 failed**（~2 min）
- 所有 24 个 MCP 工具在真实 AutoGluon 1.5.0 上验证通过
- stdout 污染已修复（两层防御，已验证 stdio 无泄漏）
- 10 项 `:full` 检查清单全部 PASS/FIXED

## Phase 1 — Tabular + stdio + 后台任务 ✅

- 镜像 `sy-automl-mcp:latest`（tabular tier，autogluon.tabular 1.5.0 + pandas 2.3.3）构建成功并可运行
- MCP server stdio 启动正常，`tools/list` 返回 24 个工具
- 端到端流程经 e2e-runner 验证：`load_dataset` (inline CSV) → `train_tabular` (returns task_id) → poll `get_task_status` until `success` → `predict_tabular` (returns predictions)
- 统一错误信封 `{success, data, error}` 已实现（`serialization/envelope.py`）
- 资源限制 `MAX_DATASET_ROWS/MB/COLUMNS` 已实现

## Phase 2 — TimeSeries / Multimodal / 模型管理 ✅

**在 `sy-automl-mcp:full` 镜像中对真实 AutoGluon 1.5.0 验证通过。**

- 完整测试套件：33 passed, 0 skipped, 0 failed（~2 min）
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

## Phase 3 — 加固 🔶 部分

- ✅ 统一 envelope（`serialization/envelope.py`）
- ✅ 资源限制（`config.py`）
- ✅ GHCR publish workflow（`.github/workflows/docker.yml`，`v*` tag 触发）
- ✅ Stdout 污染修复（两层防御）
- ❌ 进度解析增强
- ❌ CI lint pipeline
- ❌ 80% 测试覆盖率

## 技术债务（Phase 3 待处理）

1. **软取消状态竞争** — `tasks/manager.py` + `registry.py` 缺少 per-task 锁
2. **预测器缓存无上限** — `tools/model_management.py` 的 `load_model` 缓存无 LRU
3. **TaskStore 无淘汰** — 已完成任务记录无 TTL/保留策略
4. **stdout 重定向非线程安全** — 全局 `sys.stdout`/`sys.stderr` 重定向在 `max_workers > 1` 时互相干扰，需 per-worker 捕获方案（per-thread `io.StringIO` 或 logging-based）

## Git 状态

- `git init` 完成，文件已 staged
- **0 commits**（master 分支，无 commit 历史）
- Remote `git@github.com:noahwang550/sy-automl-mcp.git` 尚未配置

## 下一步

**无验证缺口。** 剩余工作：
1. Commit 所有代码 + 文档
2. 配置 remote，push 到 GitHub
3. Tag `v0.1.0`（触发 GHCR publish）
4. 处理 Phase 3 技术债务（4 项）
5. （可选）CI lint pipeline, 80% 覆盖率, 进度解析增强

## 环境备忘

- Windows 11 宿主，无原生 Python；所有执行通过 Docker
- Git Bash 需 `MSYS_NO_PATHCONV=1` 防止路径转换
- 镜像 ENTRYPOINT 是 `python server.py`；运行 pytest 需 `--entrypoint sh`
- pytest 不在生产镜像中，需运行时安装
- `:full` 镜像构建耗时 20-40+ min（CUDA/torch 下载，网络依赖）
