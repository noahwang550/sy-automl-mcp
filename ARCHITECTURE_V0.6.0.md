# ARCHITECTURE V0.6.0 — 训练报告 + 文件字节下载桥接

> 修订自 `TRAINING_REPORT_PLAN.md`。本文件只记**经代码与 API 验证后的差异决策**，原方案其余部分仍然有效。

## 0. 评审结论

| 草案章节 | 评审结论 | 说明 |
|---|---|---|
| §1 概述 | 保留 | 不变 |
| §2 架构总览 | 保留 | 组件职责划分准确 |
| §3 工具清单 | 修订 | `list_artifacts` 加 `skip`/`limit` 分页参数 |
| §4 JSON Schema | 修订 | 加 `metric_direction` 字段；`oof_metric_value` 取法不变（已验证 `fit_summary["model_performance"]` 真实存在） |
| §5 持久化 | 保留 | 不变 |
| §6 下载桥接 | 修订 | signing_key 改为 fail-closed；MAX_DOWNLOAD_BYTES 升到 1GB；`/download` 用独立线程池 |
| §7 文件改动 | 保留 | diff 形状正确 |
| §8 测试 | 修订 | 补 `metric_direction` 断言、并发不阻塞训练断言、分页断言 |
| §9 风险 | 保留 | 不变 |
| §10 实施顺序 | 保留 | 不变 |

## 1. AutoGluon API 验证（容器实测）

容器实际版本：**AutoGluon 1.6.1**（CLAUDE.md 写 1.5.0 已过期，本次顺手更正）。

| API | 签名 / 返回 | 适用 | 证据 |
|---|---|---|---|
| `TabularPredictor.fit_summary(verbosity=0)` | `-> dict`，含 `model_types`、`model_performance`(val_score per model) | tabular | `inspect.getsource(TabularPredictor.fit_summary)` |
| `TabularPredictor.leaderboard(silent=True)` | `-> pd.DataFrame`，silent 走 **kwargs | tabular | `inspect.signature` |
| `TabularPredictor.feature_importance(data, model, ..., silent=False)` | `-> pd.DataFrame`，含 `importance`、`stddev` 列 | tabular | `inspect.signature` |
| `TabularPredictor.eval_metric` | `@property -> Scorer`，有 `.name`、`.greater_is_better` | tabular | `inspect.getsource` |
| `TabularPredictor.problem_type` | `@property -> str`（"binary"/"multiclass"/"regression"） | tabular | 同上 |
| `TabularPredictor.model_best` | `@property -> str \| None` | tabular | 同上 |
| `autogluon.timeseries.TimeSeriesPredictor` | 未安装（tabular tier） | timeseries | `importlib.metadata.version('autogluon.timeseries')` → PackageNotFoundError |
| `autogluon.multimodal.MultimodalPredictor` | 未安装（tabular tier） | multimodal | 同上 |

**推论**：timeseries/multimodal 报告代码必须 lazy import（与现有 `tools/timeseries.py`、`tools/multimodal.py` 同款 `_import_predictor()` 模式），失败时 report 仍可生成但 `feature_importance` 段置 `reason="not_supported"` 或 `"predictor_unavailable"`。

## 2. 关键修订决策

### D1. signing_key fail-closed（修订草案 §6.1 第 4 级）

草案原案：无 key 时用 `secrets.token_hex(32)` 临时密钥，URL 随重启失效。

**修订**：无 key 则**禁用下载**。`/download` 返回 503 + audit；`get_artifact_url` 返回 failure envelope 提示运维配 `MCP_DOWNLOAD_SIGNING_KEY`。

**理由**：fail-open 会让 agent 平台拿到 URL 但下次重启就失效，调试体验差且语义模糊；fail-closed 强制运维显式配置。

### D2. 加 `metric_direction` 字段

`evaluation.metric_direction: "higher_is_better" | "lower_is_better"`，取自 `predictor.eval_metric.greater_is_better`。

**理由**：timeseries 的 MAPE 越小越好，方向相反。即使 AutoGluon 内部把 RMSE 转成"越大越好"的 score，agent 平台展示时要还原原始方向。

### D3. `/download` 不走独立线程池

草案原案考虑：`MCP_MAX_WORKERS=2` 时两个并发下载饿死训练。

**修订**：`/download` 是 ASGI 异步流式响应（`StreamingResponse`），不占 worker thread；worker 线程只用于 AutoGluon `fit()`。无并发冲突。

**证据**：`tasks/manager.py` 的 ThreadPoolExecutor 只跑 `_train_*_job`；ASGI I/O 走 uvicorn 事件循环。两个独立池。

### D4. `list_artifacts` 加分页

签名修订：`list_artifacts(model_id=None, subpath=None, skip=0, limit=5000)`。返回加 `next_skip`（None 表示无更多）。

**理由**：原案 5000 封顶 + `truncated=true` 但 agent 无法翻页，不可用。

### D5. `MAX_DOWNLOAD_BYTES` 升到 1GB

草案 200MB 偏紧（AutoGluon 模型常 300–800MB）。Range 单段上限同步升到 1GB。

### D6. `log_tail` 实现策略

读末 50 行：先 `f.seek(0, 2)` 拿 size，再 `f.seek(max(0, size - 8192))` 取末 8KB，按 `\n` split 取末 50 行。永不抛异常（文件缺失返回 `[]`）。

### D7. `delete_model` 时序

读 registry 条目 → 拿 `task_id` → `shutil.rmtree(model_dir)` → `log_path(task_id).unlink(missing_ok=True)`。先读后删，避免时序竞争。

### D8. `feature_importance` 信号量

加 module-level `threading.Lock()` 串行化 FI 计算（per model_id）。防止两个并发 `get_training_report(include_feature_importance=true)` 重复算同一 model。

## 3. 修订后的报告 Schema 增量

```jsonc
{
  "evaluation": {
    "metric_name": "accuracy",
    "metric_value": 0.9625,
    "metric_direction": "higher_is_better",   // ← 新增
    "oof_metric_value": 0.9413,
    ...
  }
}
```

其余字段树保持草案 §4 不变。

## 4. 修订后的 nginx 配置（DEPLOY_PLAN.md 同步更新）

```nginx
location /download {
    proxy_pass http://127.0.0.1:19885;
    proxy_buffering off;           # Range 不被打乱
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    client_max_body_size 0;
    proxy_set_header Range $http_range;
}
```

`MCP_ARTIFACT_BASE_URL` 取值规则：
- 公网 agent：`https://<公网域名或IP>:9885`
- 内网直连：`http://127.0.0.1:19885`（仅同机 agent 用）
- 空：`get_artifact_url` failure envelope

## 5. 实施顺序（一次性 commit）

1. `config.py` 加 6 个 env var（草案 5 个 + `MCP_DOWNLOAD_SIGNING_KEY` 必填化）
2. `tools/report.py` 新文件
3. `tools/artifacts.py` 新文件
4. `tools/tabular.py` 改 `_train_tabular_job`
5. `tools/timeseries.py` 改 `_train_timeseries_job`（同形，懒加载 predictor）
6. `tools/multimodal.py` 改 `_train_multimodal_job`
7. `tools/model_management.py` 改 `_delete_model` 联动清理
8. `server.py` 注册 3 个新工具 + `/download` 路由 + 豁免集
9. `tests/test_report.py`、`tests/test_artifacts.py`、`tests/test_download.py`
10. `CLAUDE.md` 工具数 28→31，env var 文档，AutoGluon 版本订正 1.5.0→1.6.1
11. `pytest tests/ -q` 全绿
12. `git commit`
13. `docker build`
14. `docker compose -f docker-compose.prod.yml up -d --force-recreate`
15. agent 平台验证

## 6. 验收清单（同草案 §11，补两条）

- [ ] 草案 §11 全部断言
- [ ] `metric_direction` 字段在 tabular 报告里非空（accuracy=higher_is_better；如果用 RMSE 训练则 lower_is_better）
- [ ] `list_artifacts(skip=0, limit=10)` 在 ≥10 条目时返回 `next_skip=10`，非 None
