# TRAINING_REPORT_PLAN.md — 训练报告 + 文件字节下载桥接 统一实施蓝图

## 1. 概述与目标

为 sy-automl-mcp 增加两个联动能力，一次性交付（目标版本 v0.6.0）：

1. **结构化训练报告**：每次 `train_*` 任务成功后在模型目录落盘一份 `report.json`（三类 predictor 共用骨架 + 类型差异子字段），并新增 `get_training_report` 工具随时取回；报告内嵌 leaderboard top-5、fit_summary、延迟计算的 feature_importance（带 >0.9 泄漏告警）、训练耗时/超时命中/提前停止等过程字段。
2. **文件字节下载桥接**：新增 `GET /download?token=<预签>&path=<artifact 相对路径>` HTTP 端点（与 streamable-http 同端口、独立于 JSON-RPC 通道，支持 Range 断点续传），配套 `get_artifact_url`（预签 URL 签发）与 `list_artifacts`（目录树）两个 MCP 工具。agent 平台由此能把 leaderboard.csv / report.json / 模型 artifact 的下载链接直接交给终端用户，补齐 `upload_dataset_chunk`/`finalize_dataset`/`/upload` 之后缺失的反向字节通道。

解决的问题：训练结果目前散落在任务日志与 predictor 内部，agent 无法给用户一份可审计、可下载、含数据质量与泄漏告警的训练报告；artifact 字节只能人工 `docker cp` 取出。

## 2. 架构总览

```
agent 平台 / Claude                     sy-automl-mcp 容器
─────────────────────  ──────────────────────────────────────────────────
train_tabular ──MCP──▶  server.py ─▶ tools/tabular.train_tabular
                        └─▶ tasks/manager.submit(_train_tabular_job)
                              │ worker 线程（stdout→task log）
                              ▼
                            AutoGluon fit()
                              │ 成功后:
                              ├─▶ registry.json (register_model, 已有)
                              ├─▶ artifacts/models/<mid>/leaderboard.csv      ← 新
                              ├─▶ artifacts/models/<mid>/fit_summary.json     ← 新
                              ├─▶ artifacts/models/<mid>/report.json          ← 新(tools/report.py)
                              └─▶ result_summary 增加 report_path 等精简字段   ← 修改
get_task_result ◀────────── {..., report_path, duration_seconds, ...}

get_training_report ─MCP─▶ tools/report.py
                              ├─ 读 artifacts/models/<mid>/report.json
                              ├─ include_feature_importance=true 且 csv 缺失
                              │    → 延迟计算 permutation FI → 写 fi csv → 回写 report.json
                              ├─ leakage 检测（importance>0.9 → warning）
                              └─ 注入 download_urls（用 tools/artifacts.py 现签，不过期烘焙）

list_artifacts ─────MCP──▶  tools/artifacts.py（白名单遍历 artifacts/ 树）
get_artifact_url ───MCP──▶  tools/artifacts.py（HMAC 预签 → 完整 URL）

浏览器/agent ──HTTP GET──▶ server.py _McpOrHealthApp
   /download?token=..&path=..      ├─ BearerTokenMiddleware: /download 免 bearer(构造参数豁免)
                                   └─ tools/artifacts.download_handler:
                                        校验预签 token(HMAC+TTL)
                                        路径白名单(resolve+is_relative_to)
                                        Range 解析 → 200/206 流式回字节
                                        审计落 audit-YYYYMMDD.jsonl
```

组件职责：

- `tools/report.py`（新）：报告装配、落盘、读取、FI 延迟计算与缓存、泄漏检测、download_urls 注入、`get_training_report` 工具本体。三类 predictor 共用一个通用工具，按 registry 里的 `type` 字段分发。
- `tools/artifacts.py`（新）：预签 token 签发/校验、路径白名单解析、`list_artifacts`/`get_artifact_url` 工具、`download_handler` ASGI 应用、下载审计。
- 三个 `_train_*_job`：训练成功后调用 report.py 落盘并扩展返回 dict。
- `server.py`：注册 3 个新工具（28→31），`_McpOrHealthApp` 增挂 `/download` 路由，构造 `BearerTokenMiddleware` 时把 `/download` 加进 `exempt_paths_all_methods`。
- `tools/auth.py`：**零代码改动**——豁免集合由 `server.py` 构造参数注入（与 `/upload` 同一机制）。
- `config.py`：新增 5 个 env var（见 §7)。

## 3. 工具与端点清单

| 名称 | 通道 | 新增/修改 | 签名 / 方法 | 入参 | 出参（envelope.data 或 HTTP) |
|---|---|---|---|---|---|
| `get_training_report` | MCP JSON-RPC | **新增** | `(model_id: str, include_feature_importance: bool = False, leaderboard_top_n: int = 0) -> dict` | `model_id` 必填；`leaderboard_top_n=0` 表示用默认 top-5；>0 时从已落盘 leaderboard.csv 重切前 N 行 | 完整报告 dict(§4),`download_urls` 为现签不过期烘焙 |
| `list_artifacts` | MCP JSON-RPC | **新增** | `(model_id: str \| None = None, subpath: str \| None = None) -> dict` | 均给定时根为 `artifacts/models/<model_id>/<subpath>`;`model_id=None` 时根为 `artifacts/`（可再配 `subpath`) | `{root, entries:[{path,type,size_bytes,mtime_iso}], total_bytes, truncated}`，条目上限 5000，超出 `truncated=true` |
| `get_artifact_url` | MCP JSON-RPC | **新增** | `(path: str, ttl_seconds: int = 3600) -> dict` | `path` 为 artifacts 相对路径（接受 `artifacts/` 前缀，内部剥离）;`ttl_seconds` 下限 60、上限 86400 | `{url, expires_at, path}`;`MCP_ARTIFACT_BASE_URL` 未配置时返回 failure envelope 提示运维配置 |
| `GET /download` | HTTP（同 MCP 端口） | **新增** | `?token=<v1.exp.agent_b64.sig>&path=<rel>`，支持 `Range: bytes=a-b` / `bytes=a-` / `bytes=-b`（单段） | 预签 token（免 bearer) | 200 全量 / 206 部分（带 `Content-Range`、`Accept-Ranges: bytes`)；错误：401/403/404/413/416 |
| `train_tabular` | MCP JSON-RPC | **修改** | 签名不变 | 不变 | 任务 `result_summary` 增加：`report_path`、`duration_seconds`、`metric_name`、`metric_value`、`time_limit_hit`、`early_stopped`（原有 `model_id/best_model/artifact_path/leaderboard_top/rows_trained/rows_dropped_empty_target` 保留） |
| `train_timeseries` | MCP JSON-RPC | **修改** | 同上 | 不变 | 同上（timeseries 版字段） |
| `train_multimodal` | MCP JSON-RPC | **修改** | 同上 | 不变 | 同上（multimodal 版字段） |
| `delete_model` | MCP JSON-RPC | **修改** | 签名不变 | 不变 | 额外删除该模型对应任务日志（registry 条目里的 `task_id` → `artifacts/logs/<task_id>.log`,best-effort)；模型目录内 report/leaderboard/fi csv 随目录删除自动清理 |
| `BearerTokenMiddleware` | — | **修改（仅 server.py 调用点）** | 构造参数 `exempt_paths_all_methods` 增加 `"/download"` | — | `/download` 免 bearer；其 handler 自行强校验预签 token |

工具总数：28 → **31**。

## 4. 报告 JSON Schema 完整字段树

记法：`字段: 类型 | 填充时机 | 可空 | 示例 | 适用范围`。填充时机：**T**=训练时写入并持久化到 report.json;**S**=`get_training_report` 服务时注入（不落盘）;**L**=FI 延迟计算时回写。

```jsonc
{
  "schema_version": 1,              // int    | T | 否 | 1 | 全部。reader 遇到未知更高版本仍返回并加 "warning"
  "model_id": "m01",                // str    | T | 否 | "m01" | 全部
  "task_id": "t_abc...",            // str    | T | 否 | task.task_id | 全部
  "status": "success",              // str    | T | 否 | "success"(report 只在成功时写) | 全部
  "started_at": "2026-09-10T...",   // ISO8601| T | 否 | 取自 task.started_at | 全部
  "finished_at": "...",             // ISO8601| T | 否 | fit 完成时刻 | 全部
  "duration_seconds": 733,          // float  | T | 否 | finished-started | 全部
  "predictor_type": "tabular",      // str    | T | 否 | tabular|timeseries|multimodal | 全部

  "data_summary": {
    "dataset_id": "d01",                    // str | T | 否 | params.dataset_id | 全部
    "rows_raw": 10000,                      // int | T | 否 | 读入总行数 | 全部
    "rows_dropped_empty_target": 9200,      // int | T | 否 | 空 target 剔除数;ts/mm 无此逻辑时置 0 | 全部
    "rows_trained": 800,                    // int | T | 否 | 实际入 fit 行数 | 全部
    "columns_total": 12,                    // int | T | 否 | df.columns 数 | 全部
    "target": "segment",                    // str | T | 否 | tab/ts:params.target; mm:params.label | 全部
    "target_classes": ["A","B","C","D"],    // list[str]|T| 可空 | 分类时为 df[target] 去重排序(截断前 50);regression/ts/mm 为 null | tabular
    "target_distribution": {"A":200,...},   // dict[str,int]|T| 可空 | 分类时 value_counts(截断前 50 类);否则 null | tabular
    "problem_type": "multiclass",           // str | T | 可空 | predictor.problem_type;"binary|multiclass|regression" | tabular(mm 复用此字段放 task 类型亦可,见下)
    "excluded_columns": [],                 // list[str]|T| 否 | 默认 [];预留 | 全部
    "prediction_length": 24,                // int | T | 可空 | params.prediction_length | timeseries
    "freq": "H",                            // str | T | 可空 | params.freq(或推断值) | timeseries
    "image_column": "img_path",             // str | T | 可空 | params.image_path_column | multimodal
    "text_column": null,                    // str | T | 可空 | params.text_column | multimodal
    "task_type": "classification"           // str | T | 可空 | params.problem_type 或 predictor 推断 | multimodal
  },
  // 不适用的类型差异字段一律序列化为 null(字段保留,保证 schema 形状稳定)

  "training_config": {
    "presets": "medium_quality",     // str | T | 可空 | params.presets | tab/mm(ts 为 presets 或 None)
    "eval_metric": "accuracy",       // str | T | 可空 | 实际生效 metric(predictor.eval_metric) | 全部
    "time_limit": 600,               // int | T | 可空 | params.time_limit(秒) | 全部
    "hyperparameters": null,         // obj | T | 可空 | params.hyperparameters 原样 | 全部
    "random_seed": null,             // int | T | 可空 | params.random_seed | 全部
    "verbosity": 0,                  // int | T | 否 | 恒 0(本项目约定) | 全部
    "autogluon_version": "1.5.0"     // str | T | 否 | importlib.metadata.version("autogluon") | 全部
  },

  "training_process": {
    "models_attempted": 12,    // int | T | 否 | len(leaderboard)(含失败行) | 全部
    "models_trained": ["LightGBM",...], // list[str] | T | 否 | leaderboard["model"].tolist() | 全部
    "time_limit_hit": true,    // bool| T | 否 | 启发式:duration>=0.98*time_limit 或任务日志含 "Time limit" | 全部
    "early_stopped": false,    // bool| T | 否 | 启发式:任务日志含 "Early stopping" | 全部
    "log_tail": ["...", ...]   // list[str] | T | 否 | 任务日志末 50 行(截断) | 全部
  },

  "evaluation": {
    "best_model": "LightGBM",        // str | T | 可空 | predictor.model_best | 全部
    "metric_name": "accuracy",       // str | T | 可空 | predictor.eval_metric | 全部
    "metric_value": 0.9625,          // float| T | 可空 | leaderboard 中 best_model 行 score_val(AutoGluon 已统一为越大越好) | 全部
    "oof_metric_value": 0.9413,      // float| T | 可空 | fit_summary["model_performance"][best_model](bagged 时即 OOF);取不到为 null | 全部
    "leaderboard_top": [...],        // list[obj] | T(默认5行)/S(leaderboard_top_n>0 时从 csv 重切) | 否 | to_jsonable(lb.head(5)) | 全部
    "fit_summary": {...}             // obj | T | 否 | predictor.fit_summary(verbosity=0) 经 to_jsonable | 全部
  },

  "feature_importance": null,        // obj|null | T=常驻 null,L=计算后回写 | 可空 | 见下 | 全部(mm 恒 null+reason)
  // 计算后形状:
  // {
  //   "method": "permutation",                       // str | L | 否
  //   "computed_at": "...",                          // ISO | L | 否
  //   "top_features": [{"feature":"is_seed","importance":0.95,"std":0.01,"n":...}, ...], // top-20
  //   "leakage_warning": "Feature 'is_seed' has importance 0.95 (> 0.9) — suspect label leakage" | null,
  //   "full_path": "artifacts/models/m01/feature_importance.csv",
  //   "download_url": "https://.../download?token=...&path=...",   // S 注入,不落盘
  //   "reason": null | "not_supported_for_multimodal" | "dataset_unavailable" | "compute_failed: ..."
  // }

  "artifacts": {                     // 全部为 str 路径,T 填充并持久化
    "model_dir": "artifacts/models/m01",
    "predictor_path": "artifacts/models/m01",          // AutoGluon predictor 目录(=model_path)
    "leaderboard_csv": "artifacts/models/m01/leaderboard.csv",
    "fit_summary_json": "artifacts/models/m01/fit_summary.json",
    "feature_importance_csv": "artifacts/models/m01/feature_importance.csv", // 路径预登记;文件可能尚未生成
    "report_json": "artifacts/models/m01/report.json",
    "training_log": "artifacts/logs/<task_id>.log"
  },

  "download_urls": {                 // 全部 S 注入(现签 TTL token),不落盘
    "report_json": "https://host:9885/download?token=v1....&path=models/m01/report.json",
    "leaderboard_csv": "...",
    "fit_summary_json": "...",
    "feature_importance_csv": "...",   // 文件不存在时仍签发,下载时 404(agent 可据 feature_importance==null 预判)
    "training_log": "...",
    "model_archive": "...path=models/m01.tar.gz"       // 虚拟路径,handler 动态打 tar.gz 流
  },

  "next_actions": [                   // list[str] | T | 否 | 模板生成,规则见下
    "Review leakage_warning before trusting metrics",
    "Download leaderboard_csv via download_urls",
    "Evaluate on a holdout dataset with evaluate_tabular"
  ]
}
```

`next_actions` 生成规则（按序拼接，去重）:
- `leakage_warning` 非空 → 置顶泄漏复核建议。
- `time_limit_hit=true` → 建议加大 `time_limit` 或升 presets 重训。
- 通用尾巴：下载 leaderboard / 在留出集 evaluate / 用 `predict_*` 推理（按 predictor_type 选措辞）。

## 5. 持久化策略

| 文件 | 写入时机 | 大小量级 | 生命周期 |
|---|---|---|---|
| `artifacts/models/<mid>/leaderboard.csv` | `_train_*_job` 成功尾部 | 1–50 KB | 随 `delete_model` 删目录 |
| `artifacts/models/<mid>/fit_summary.json` | 同上（`to_jsonable` 后写，tmp+replace 原子写） | 5–200 KB | 同上 |
| `artifacts/models/<mid>/report.json` | 同上（只存 T/L 字段；`download_urls` 与 `feature_importance.download_url` **不落盘**) | 10–80 KB(log_tail ≤50 行封顶） | 同上；FI 回写时整文件 tmp+replace 重写 |
| `artifacts/models/<mid>/feature_importance.csv` | 首次 `get_training_report(include_feature_importance=true)` | 1–20 KB | 同上；已存在则跳过计算（缓存） |
| `artifacts/logs/<task_id>.log` | 训练全程（现有机制） | 0.1–50 MB | `delete_model` 联动删除（新增，best-effort) |

- 所有 JSON 写盘统一 `tmp.write → replace`（与 `config._write_registry` 同款），防半写文件。
- 磁盘上限：不新增全局配额——报告类文件 KB 级，模型本体才是大头，沿用现有 `delete_model` + registry 治理即可；任务日志删除纳入 `delete_model` 后不再无限累积（此前只清任务记录不清 log 文件）。
- 向后兼容：v0.6.0 之前训练的模型没有 `report.json`,`get_training_report` 返回 failure envelope:`"report.json not found for model '<mid>' (trained before v0.6.0); retrain to generate a report"`，并附 registry 条目供排查。不做自动重建（避免为旧模型隐式加载数 GB predictor)。

## 6. 下载桥接详细设计

### 6.1 预签 token

- 格式：`v1.<expires_at_unix>.<b64url(agent_id)>.<hex_sig>`(`b64url` 无 padding)。
- 签名：`sig = HMAC_SHA256(signing_key, f"download|{path}|{expires_at}|{agent_id}")`,`path` 为**规范化后的 artifacts 相对路径**（与 query 参数 `path` 完全一致比对，防篡改）。
- `signing_key` 解析顺序：
  1. `MCP_DOWNLOAD_SIGNING_KEY` env（显式配置，推荐生产）;
  2. 多 token 模式：`sha256(tokens.json 原始文件字节)`(TokenStore 热重载时同步失效旧 URL，可接受）;
  3. 单 token 模式：`MCP_API_TOKEN` 本体；
  4. 全无鉴权（auth disabled)：进程启动时 `secrets.token_hex(32)` 临时密钥，URL 随重启失效，启动日志 warning 一行。
- 校验（`verify_presigned_token(token, path) -> (ok, agent_id_or_reason)`):split 4 段 → 版本必须 `v1` → b64 解 agent_id → 重算 HMAC 用 `hmac.compare_digest` 恒定时间比对 → `expires_at < now` 拒绝。任一失败返回 401，不区分原因。

### 6.2 路径白名单（`resolve_artifact_path`）

```python
def resolve_artifact_path(path: str) -> Path:
    p = (path or "").strip().lstrip("/")
    if p.startswith("artifacts/"): p = p[len("artifacts/"):]
    if not p or p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise ValueError("absolute paths rejected")
    parts = p.split("/")
    for seg in parts:
        validate_id(seg, "path_segment")   # 复用 config 的白名单正则,天然拒绝 ".." / 反斜杠
    resolved = (ARTIFACTS_DIR / Path(*parts)).resolve()   # resolve 同时压平符号链接
    if not resolved.is_relative_to(ARTIFACTS_DIR.resolve()):
        raise ValueError("path escapes artifacts root")
    return resolved
```

符号链接逃逸经 `resolve()` 后被 `is_relative_to` 拦截 → 403。虚拟归档路径例外：`models/<model_id>.tar.gz`（指向目录的归档，磁盘上不存在该文件）单独分支处理，同样先过上面的段校验。

### 6.3 Range 与流式响应

- 用 `starlette.responses.StreamingResponse` 手动实现（Starlette `FileResponse` 不支持 Range)。块大小 64 KB。
- 支持三种单段语法：`bytes=a-b`、`bytes=a-`、`bytes=-b`(suffix)。多段（逗号）直接 416。
- 解析后 `start>end` 或 `start>=size` → 416 + `Content-Range: bytes */<size>`。
- 206 响应头：`Content-Range: bytes <s>-<e>/<size>`、`Content-Length`、`Accept-Ranges: bytes`；全量 200 也带 `Accept-Ranges: bytes` 宣告能力。
- HEAD：与 GET 同逻辑但不流 body（顺手支持，便于 agent 探测大小）。
- 上限：无 Range 且 `size > MAX_DOWNLOAD_BYTES` → 413;Range 段长 > `MAX_DOWNLOAD_BYTES` → 413。虚拟 tar.gz：先 `directory_size_bytes()` 预估，超限 413（压缩后实际更小，预估偏保守，文档化）。

### 6.4 Content-Type 推断表

`.csv→text/csv; charset=utf-8`,`.json→application/json`,`.log/.txt→text/plain; charset=utf-8`,`.html→text/html`,`.pkl/.pt/.bin/.model/.parquet→application/octet-stream`,`.png/.jpg/.jpeg/.gif/.webp→image/*`,`.gz/.tgz→application/gzip`,`.tar.gz→application/gzip`,`.zip→application/zip`，其余 → `application/octet-stream`。并附 `Content-Disposition: attachment; filename="<basename>"`（保证浏览器落盘而非预览，csv/json 也不例外）。

### 6.5 审计

复用 `MCP_AUDIT_LOG_DIR/audit-YYYYMMDD.jsonl`（未配置则仅 stdout)，字段：`{ts, event:"download", agent_id, path, bytes, status, ip, range, duration_ms}`。所有出口（200/206/401/403/404/413/416）都写一行；`agent_id` 取自预签 token，解不出记 `"anonymous"`。

### 6.6 错误码

401 预签缺失/无效/过期；403 路径越权（绝对路径、`..`、符号链接逃逸）;404 文件不存在或 `MCP_DOWNLOAD_ENABLED=false`（故意混淆为 404);413 超 `MAX_DOWNLOAD_BYTES`;416 Range 不可满足。

### 6.7 env var 默认值

`MAX_DOWNLOAD_BYTES=209715200`(200MB)、`MCP_DOWNLOAD_ENABLED=true`（预签+只读+TTL，默认开安全；上传默认关是因为它是写入口）、`MCP_ARTIFACT_BASE_URL=""`（空时 `get_artifact_url` 返回 failure 提示配置）、`MCP_DOWNLOAD_SIGNING_KEY=""`（空则按 §6.1 派生）、`MCP_DOWNLOAD_URL_TTL_SECONDS=3600`（工具入参默认值取它）。

## 7. 文件改动清单

### 7.1 `config.py`（新增 5 个常量）

在 `MCP_UPLOAD_URL_BASE` 之后追加：

```python
MCP_DOWNLOAD_ENABLED = os.environ.get("MCP_DOWNLOAD_ENABLED", "true").lower() in {"1","true","yes","on"}
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(200 * 1024 * 1024)))
MCP_ARTIFACT_BASE_URL = os.environ.get("MCP_ARTIFACT_BASE_URL", "").rstrip("/").strip()
MCP_DOWNLOAD_SIGNING_KEY = os.environ.get("MCP_DOWNLOAD_SIGNING_KEY") or None
MCP_DOWNLOAD_URL_TTL_SECONDS = max(60, int(os.environ.get("MCP_DOWNLOAD_URL_TTL_SECONDS", "3600")))
```

### 7.2 `tools/report.py`（新文件，~300 行）

```python
SCHEMA_VERSION = 1
LEAKAGE_THRESHOLD = 0.9
LOG_TAIL_LINES = 50
TOP_FEATURES_N = 20

def _json_atomic_write(path: Path, obj) -> None          # tmp+replace
def _iso(ts: float) -> str
def _read_log_tail(log_path: str, n: int) -> list[str]   # 缺失→[];永不抛
def _detect_log_flags(log_path, duration, time_limit) -> tuple[bool, bool]  # (time_limit_hit, early_stopped) 启发式
def detect_leakage(fi_df) -> str | None                  # 任一 importance>0.9 → 文案含特征名与阈值
def compute_feature_importance(model_id, predictor_type, dataset_id, target) -> dict
    # mm → {"reason": "not_supported_for_multimodal"};csv 已存在 → 读缓存直接返回;
    # tabular: predictor.feature_importance(df, silent=True)(需数据集仍在,缺 → reason="dataset_unavailable")
    # timeseries: 同上,TimeSeriesPredictor.feature_importance 存在性 getattr 探测,失败 reason="compute_failed: <e>"
    # 成功: 写 feature_importance.csv,返回 {method, computed_at, top_features(≤20), leakage_warning, full_path, reason: None}
def assemble_report(*, predictor_type, task, params, data_summary, training_config_extra,
                    predictor, leaderboard, fit_summary, duration_seconds) -> dict   # 纯函数,返回完整 T 段 dict
def persist_report(model_id, report) -> str              # 原子写 report.json,返回 str 路径
def load_report(model_id) -> dict | None                 # 读+校验 schema_version
def build_download_urls(artifacts: dict[str, str], agent_id: str) -> dict[str, str]  # 对每条 path 调 artifacts.build_presigned_token;另加 model_archive 虚拟路径;MCP_ARTIFACT_BASE_URL 未配 → 该节整体为 {} 且报告带 "download_urls_note"
def _get_training_report(model_id, include_feature_importance=False, leaderboard_top_n=0) -> dict
    # validate_id → load_report(None→抛 FileNotFoundError 由 safe_tool 转 failure)
    # leaderboard_top_n>0: 读 leaderboard.csv 重切替换 evaluation.leaderboard_top
    # include_feature_importance 且 feature_importance 段为 null → compute_feature_importance → 回写 report.json → 用新段
    # 注入 download_urls + feature_importance.download_url(current_agent_id.get())
def get_training_report(...) = safe_tool 包装后导出
```

### 7.3 `tools/artifacts.py`（新文件，~320 行）

```python
PRESIGN_VERSION = "v1"; _CHUNK = 64 * 1024; _LIST_CAP = 5000
CONTENT_TYPES = {...}  # §6.4 表
def _signing_key() -> bytes                            # §6.1 四级回退
def _b64e/_b64d(s: str)                                # urlsafe nopad
def build_presigned_token(path, expires_at, agent_id) -> str
def verify_presigned_token(token, path) -> tuple[bool, str]
def resolve_artifact_path(path) -> Path                # §6.2;抛 ValueError→403
def _audit_download(scope, agent_id, path, nbytes, status, started, range_hdr)  # 写 jsonl+stdout
def _list_artifacts(model_id, subpath) -> dict         # 根解析(同白名单)→ os.scandir 递归,收集 {path,type,size_bytes,mtime_iso},封顶 5000
def _get_artifact_url(path, ttl_seconds) -> dict       # resolve 校验存在性(虚拟归档除外)→ 拼 {MCP_ARTIFACT_BASE_URL}/download?token=..&path=..(urlencode)
async def download_handler(scope, receive, send)       # §6 全流程;内部 async def _stream(...) 按 Range seek+读块
list_artifacts  = safe_tool(...); get_artifact_url = safe_tool(...)
```

### 7.4 `tools/tabular.py`（修改 `_train_tabular_job`，其余不动）

在现有 `register_model(...)` 之后、`return` 之前：

```python
from tools import report as report_mod
finished = time.time()
lb = predictor.leaderboard(silent=True)
fs = predictor.fit_summary(verbosity=0)
(model_path(p["model_id"]) / "leaderboard.csv").write_text(lb.to_csv(index=False), encoding="utf-8")
report_mod._json_atomic_write(model_path(p["model_id"]) / "fit_summary.json", to_jsonable(fs))
is_clf = predictor.problem_type in ("binary", "multiclass")
vc = df[target].value_counts()
data_summary = {
    "dataset_id": p["dataset_id"], "rows_raw": rows_before,
    "rows_dropped_empty_target": rows_dropped_empty_target, "rows_trained": len(df),
    "columns_total": len(df.columns), "target": target,
    "target_classes": sorted(map(str, vc.index))[:50] if is_clf else None,
    "target_distribution": {str(k): int(v) for k, v in vc.head(50).items()} if is_clf else None,
    "problem_type": predictor.problem_type,
}
report = report_mod.assemble_report(predictor_type="tabular", task=task, params=p,
    data_summary=data_summary, training_config_extra={}, predictor=predictor,
    leaderboard=lb, fit_summary=fs, duration_seconds=finished - task.started_at)
report_path = report_mod.persist_report(p["model_id"], report)
```

返回 dict 在现有 6 个键上增加：`"report_path": report_path`、`"duration_seconds": round(...,1)`、`"metric_name"`、`"metric_value"`（取 report.evaluation)、`"time_limit_hit"`、`"early_stopped"`（取 report.training_process)。

### 7.5 `tools/timeseries.py` / 7.6 `tools/multimodal.py`

同 7.4 形状，差异仅在 `data_summary`:timeseries 填 `prediction_length=p["prediction_length"]`、`freq=p.get("freq")`,`target_classes/distribution/problem_type=None`;multimodal 的 `target=p["label"]`、`image_column=p.get("image_path_column")`、`text_column=p.get("text_column")`、`task_type=p.get("problem_type")`。leaderboard/fit_summary 两 predictor 都有同名方法，直接调用；`to_jsonable` 处理 numpy 类型。

### 7.7 `server.py`

```python
from tools.report import get_training_report
from tools.artifacts import list_artifacts, get_artifact_url, download_handler
# 注册 tuple 末尾追加 3 个 → 31 个工具
# _McpOrHealthApp.__init__: self._download_enabled = MCP_DOWNLOAD_ENABLED
# __call__ 在 /upload 分支后、mcp_app 兜底前加:
#   if self._download_enabled and path == "/download":
#       await download_handler(scope, receive, send); return
# BearerTokenMiddleware(..., exempt_paths_all_methods=({"/upload"} if MCP_UPLOAD_ENABLED else set()) | {"/download"})
```

注意：`/download` 始终进豁免集（即使 `MCP_DOWNLOAD_ENABLED=false`)，此时由 `_McpOrHealthApp` 不落分支、透传到 MCP app 后 404，行为一致。

### 7.8 `tools/auth.py`

**零改动**。豁免由 server.py 构造参数注入（与 `/upload` 机制相同）。

### 7.9 `tools/model_management.py`(`_delete_model` 联动）

删除模型目录成功后，读（删除前的）registry 条目 `task_id`,`log_path(task_id).unlink(missing_ok=True)`,`OSError` 仅 log 不失败。模型目录内 report/leaderboard/fi csv 随 `shutil.rmtree` 自动清理。

### 7.10 测试与文档

- `tests/test_report.py`、`tests/test_artifacts.py`、`tests/test_download.py` 新建（§8)。
- `CLAUDE.md`：工具数 28→31；新增 5 个 env var 条目；新增"下载桥接"小节（预签机制、白名单、Range、错误码）;`get_training_report` 一句话说明。
- 版本标记 v0.6.0(git commit message 用 `feat: training report + artifact download bridge`)。

## 8. 测试用例清单（26 条）

**tests/test_report.py**(AutoGluon 重活用 monkeypatch 打桩：假 `_import_predictor` 返回带 `problem_type/eval_metric/model_best/leaderboard()/fit_summary()/feature_importance()` 的 stub):

1. `test_report_tabular_schema` — report.json 含全部顶层键、`schema_version==1`、`predictor_type=="tabular"`。
2. `test_train_compact_return_fields` — job 返回含 `report_path/duration_seconds/metric_name/metric_value/time_limit_hit/early_stopped`，且 `report_path` 文件存在。
3. `test_report_target_classes_and_distribution` — 分类数据的类别列表与计数正确；回归数据两字段为 null。
4. `test_report_rows_dropped_empty_target` — 与训练时剔除数一致。
5. `test_report_timeseries_fields` — `prediction_length/freq` 正确，tabular 专属字段为 null。
6. `test_report_multimodal_fields` — `image_column/text_column/task_type` 正确。
7. `test_feature_importance_lazy_default` — 训练后 `feature_importance` 段为 null、csv 不存在。
8. `test_feature_importance_computed_on_demand` — `get_training_report(..., include_feature_importance=true)` 后 csv 存在、段内 `top_features ≤20`、report.json 已回写。
9. `test_feature_importance_cached` — 第二次调用 csv mtime 不变（不重复计算）。
10. `test_leakage_warning_triggered` — stub FI 含 0.95 → `leakage_warning` 非空且含特征名与 "0.9"。
11. `test_leakage_warning_absent` — 全部 <0.9 → null。
12. `test_leaderboard_top_default_5` — 默认返回 ≤5 行。
13. `test_leaderboard_top_n_override` — `leaderboard_top_n=3` → 3 行，且不改写磁盘 report.json 里的默认 5 行。
14. `test_report_download_urls_injected_not_persisted` — 响应 `download_urls` 非空且 token 可校验；磁盘 report.json 无 `download_urls` 键。
15. `test_report_time_fields` — `duration_seconds>0`、`started_at/finished_at` ISO 可解析、`time_limit` 原样。
16. `test_report_missing_for_legacy_model` — 手工建空模型目录 → failure envelope 含 "report.json not found"。
17. `test_multimodal_fi_reason` — mm 模型 `include_feature_importance=true` → `reason=="not_supported_for_multimodal"`。

**tests/test_artifacts.py**:

18. `test_presigned_roundtrip` — build→verify 通过，agent_id 还原正确。
19. `test_presigned_tamper_path` — 同 token 换 path → 校验失败。
20. `test_presigned_expired` — `expires_at` 过去时 → 失败。
21. `test_resolve_rejects_traversal` — `../../etc/passwd`、`models/../..`、绝对路径、反斜杠 → ValueError。
22. `test_resolve_rejects_symlink_escape` — artifacts 内搭指向 /etc 的软链 → ValueError。
23. `test_list_artifacts_tree` — 造文件后返回 size/mtime 正确；`model_id` 限定根；`subpath` 下钻；超 5000 条目 `truncated=true`。
24. `test_get_artifact_url_requires_base` — `MCP_ARTIFACT_BASE_URL=""` → failure envelope 提示配置。

**tests/test_download.py**（直接以 ASGI 调 `download_handler`，用 dict scope + 收集 send 事件）:

25. `test_download_full_200` — 字节一致、Content-Type 按后缀、`Accept-Ranges: bytes`、`Content-Disposition: attachment`。
26. `test_download_range_206` — `bytes=0-99` → 206、100 字节、`Content-Range: bytes 0-99/<size>`;suffix `bytes=-50` 与开口 `bytes=50-` 各一断言。
27. `test_download_range_416` — 越界 Range → 416 + `Content-Range: bytes */<size>`。
28. `test_download_401_invalid_token` / `test_download_403_traversal` / `test_download_404_missing`。
29. `test_download_413_oversize` — monkeypatch `MAX_DOWNLOAD_BYTES=10` → 413。
30. `test_download_audit_line` — 成功后 audit jsonl 新增一行含 `event=="download"`、path、bytes、status=200、agent_id 与签发时一致。
31. `test_download_no_bearer_middleware_passthrough` — 组 `BearerTokenMiddleware(exempt_paths_all_methods={"/download"})` 包 download_handler，无 Authorization + 有效预签 → 200；无效预签 → 401(handler 自拒）。
32. `test_delete_model_cleans_report_and_log` — 造 report.json/leaderboard.csv/feature_importance.csv/任务日志 → `delete_model(confirm=true)` 后全部不存在。

## 9. 风险与权衡

| 风险 | 缓解 |
|---|---|
| 预签 URL 泄漏后 TTL 内任何人可下载 | TTL 默认 1h、上限 24h；只读；审计留 IP;`MCP_DOWNLOAD_SIGNING_KEY` 轮换即全量失效 |
| 多 token 模式下 signing key 派生自 tokens.json，热更新会使旧 URL 失效 | 文档化；生产建议显式配 `MCP_DOWNLOAD_SIGNING_KEY` 与 tokens.json 解耦 |
| FI 延迟计算仍需加载 predictor（数 GB)+ 原始数据集 | 走 `_ModelLRUCache`；数据集缺失时降级 `reason` 不报错；mm 直接不支持 |
| `time_limit_hit`/`early_stopped` 是日志启发式，可能误判 | 字段文档标注 "best-effort heuristic"；同时给出 `duration_seconds` 与 `time_limit` 原始值供 agent 自行判断 |
| tar.gz 虚拟归档大小不可预知，超限只能按未压缩预估 413 | 预估偏保守（拒绝可下载的临界大模型），文档化；Range 对归档禁用（416) |
| `/download` 免 bearer 扩大攻击面 | 强预签 + 路径白名单 + 恒定时间比对 + 401 不区分原因；`MCP_DOWNLOAD_ENABLED=false` 一键关停（404 混淆） |
| 报告字段膨胀导致 report.json 过大 | log_tail ≤50 行、leaderboard 默认 5 行、FI top-20 封顶；完整数据在 csv/json 旁文件 |
| 旧模型无 report.json | 明确 failure 文案 + 重训提示，不做隐式重建 |

## 10. 一次性实施步骤（单次提交）

1. `config.py` 加 5 个 env var(§7.1)。
2. 写 `tools/report.py`(§7.2）与 `tools/artifacts.py`(§7.3）两个共用模块。
3. 改 `tools/tabular.py`、`tools/timeseries.py`、`tools/multimodal.py` 的 `_train_*_job`：落盘三件套 + 扩展返回 dict(§7.4–7.6)。
4. `tools/model_management.py` 的 `_delete_model` 加任务日志联动清理（§7.9)。
5. `server.py`：注册 3 个新工具、`/download` 路由、豁免集加 `/download`(§7.7)。
6. 写 `tests/test_report.py`、`tests/test_artifacts.py`、`tests/test_download.py`(§8)。
7. `CLAUDE.md`：28→31、env var 文档、下载桥接小节（§7.10)。
8. `docker run --rm --entrypoint sh -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp -c "pip install pytest pytest-asyncio -q && python -m pytest tests/ -q"` 全绿。
9. `docker build` 重建镜像 → 重启容器 → agent 平台按 §11 验收。

## 11. 验收清单

- [ ] `train_tabular` 完成后 `get_task_result` 返回含 `report_path`、`duration_seconds`、`metric_name/metric_value`、`time_limit_hit`、`early_stopped`，磁盘上 `artifacts/models/<mid>/report.json`、`leaderboard.csv`、`fit_summary.json` 三件存在。
- [ ] `get_training_report(model_id="m01")` 返回完整报告；`include_feature_importance=true` 首次触发 FI 计算并落 csv，二次调用走缓存。
- [ ] `leaderboard_top_n=3` 时响应 `evaluation.leaderboard_top` 恰 3 行，默认 5 行。
- [ ] `list_artifacts(model_id="m01")` 返回带 size/mtime 的文件树；`list_artifacts()` 返回全 artifacts 树。
- [ ] `get_artifact_url(path="artifacts/models/m01/leaderboard.csv")` 返回 `{url, expires_at}`；浏览器无 header 直接打开 URL 即下载字节；`Range: bytes=0-99` 得 206。
- [ ] 篡改 token / 过期 token / `../` 路径 / 超 200MB 分别得 401/401/403/413;audit jsonl 每次下载一行含 agent_id+path+bytes+status。
- [ ] 100% accuracy 案例（含 `is_seed` 类泄漏特征）报告中 `feature_importance.leakage_warning` 非空，点名特征与 0.9 阈值；`next_actions` 首条即泄漏复核建议。
- [ ] 报告 `download_urls` 各链接与 `get_artifact_url` 产出同构，且磁盘 report.json 中不含 `download_urls`（现签不落盘）。
- [ ] `delete_model("m01", confirm=true)` 后 report.json / leaderboard.csv / fit_summary.json / feature_importance.csv / 任务日志全部清除。
- [ ] v0.6.0 之前训练的模型调 `get_training_report` 返回带 "retrain" 提示的 failure envelope，不崩溃。
- [ ] stdio 模式全程 stdout 无污染（`e2e_stdio.py` 通过，工具数断言更新为 31)。
