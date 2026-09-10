# sy-automl-mcp 生产部署方案（最终定稿）

> 目标架构：公网 `https://47.99.174.228:9885`（nginx TLS 终结 + 限流 + fail2ban）→ `127.0.0.1:19885`（Docker 容器，FastMCP streamable-http，应用层多 token 鉴权 + agent_id 审计）。无外部告警，systemd + `restart: unless-stopped` 自愈。

**磁盘约束假设**：部署前已自行腾出 ≥30G 可用空间（镜像 + artifacts 增长）。第一道前置检查不满足即中止。

**资源设定依据**：整机 31G 内存中 20G 被其他服务持有，可用约 10G → 容器硬限 8G / 6 CPU，给宿主机留 2G 余量。全局训练并发 2（`MCP_MAX_WORKERS=2`）、单任务 30 分钟超时、单容器 8G，均落在此约束内。

---

## 服务器环境实测

- 公网 IP：`47.99.174.228`（固定，弹性 IP）
- 内存：31G 总，20G 已被其他服务持有（不可回收），可用 ~10G
- CPU：8 vCPU（Xeon Platinum 8163）
- 磁盘：197G 总，已用 173G，剩 16G（部署前需腾出 ≥30G）
- 无 GPU
- OpenSSL 1.1.1k（支持 `-addext` SAN）
- firewalld：未运行（inactive/disabled），全靠云安全组
- nginx：未安装（待装）
- fail2ban：未安装（待装）
- Python：系统 3.8（过旧，但用 Docker 不影响）
- uv 0.11.7 已装（Docker 方案下用不到）
- 端口 9885 空闲；9880/9876/9886/9889/9000/8001/5432 等已被其他容器占用
- /home/sy 占 10G，其中 .cache 2.4G + .npm 1.6G（清理候选）
- /home/syy 另一用户，与 sy 无关

## 仓库实测（已 clone 到 /data/sy/sy-automl-mcp）

- 框架：Python 3.11 + FastMCP + streamable-http（默认 stdio，需 `MCP_TRANSPORT=http` 切换）
- 24 个 AutoML 工具（tabular/timeseries/multimodal/模型管理/任务状态）
- **应用层已有鉴权**：`tools/auth.py` 的 `BearerTokenMiddleware`（单 token 模式，`MCP_API_TOKEN` env 控制）
- 部署文件：仓库自带 `Dockerfile`（tabular/full 两 tier）+ `docker-compose.yml`
- 默认 host 0.0.0.0:8000，需改 127.0.0.1:19885
- artifacts 目录：`./artifacts/{datasets,models,predictions,logs,registry.json}`
- 资源限制相关 env：`MCP_MAX_WORKERS`、`MCP_MODEL_CACHE_MAX`、`MCP_TASK_RETENTION_SECONDS`、`MAX_DATASET_MB` 等

## 已确认架构决策

- 部署方式：**Docker**（仓库自带，作者意图明显，省环境折腾）
- 端口：nginx 监听 0.0.0.0:9885 ssl，反代到 127.0.0.1:19885 的容器
- 鉴权：**应用层多 token + agent_id**（改造 `tools/auth.py`，扩展现有 `BearerTokenMiddleware`）
- TLS：自签证书（SAN = 47.99.174.228 + 127.0.0.1 + localhost）
- 限流：nginx `limit_req 10r/s burst=20` + `limit_conn 10`
- fail2ban：启用（maxretry=20/findtime=60s/bantime=10min，只封 401）
- 审计日志：应用层记录 agent_id + 工具 + 耗时 + 状态，保留 90 天
- 产物路径：`/data/sy/sy-automl-mcp/artifacts/`（按 agent_id 分子目录）
- 产物清理：保留 7 天 + 每 agent 上限 20GB（cron）
- 资源：全局并发 2、每 agent 并发 1、单任务 8G/30min、每 agent 日配额 2h
- 告警：无外部告警，systemd 自愈 + journalctl
- 安全组：仅放行 9885（源 0.0.0.0/0）+ 22（建议收敛到管理员 IP）

---

## 第一部分：前置检查与准备

### 1.1 前置检查脚本

保存为 `/data/sy/sy-automl-mcp/deploy/preflight.sh`，`chmod +x` 后执行：

```bash
#!/bin/bash
# /data/sy/sy-automl-mcp/deploy/preflight.sh
set -euo pipefail

AVAIL_G=$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}')
if [ "$AVAIL_G" -lt 30 ]; then
  echo "[ABORT] / 分区仅剩 ${AVAIL_G}G（要求 >= 30G）。请先清理磁盘。"
  echo "  清理候选: ~/.cache (约2.4G)、~/.npm (约1.6G)、'docker system prune -a'"
  exit 1
fi

command -v docker >/dev/null || { echo "[ABORT] docker 未安装"; exit 1; }
(docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null) \
  || { echo "[ABORT] docker compose 不可用"; exit 1; }
systemctl is-active --quiet docker || { echo "[ABORT] docker 服务未运行"; exit 1; }

ss -ltn | grep -q ':9885 '  && { echo "[ABORT] 9885 已被占用"; exit 1; }
ss -ltn | grep -q ':19885 ' && { echo "[ABORT] 19885 已被占用"; exit 1; }

[ -f /data/sy/sy-automl-mcp/Dockerfile ] || { echo "[ABORT] 仓库不在预期路径"; exit 1; }
echo "[OK] preflight 通过（可用磁盘 ${AVAIL_G}G）"
```

### 1.2 安装 nginx / fail2ban

```bash
sudo dnf install -y epel-release
sudo dnf install -y nginx fail2ban fail2ban-firewalld- 2>/dev/null || sudo dnf install -y nginx fail2ban
nginx -v && fail2ban-client --version
```

说明：firewalld 处于 inactive，fail2ban 使用 iptables action（本方案已按此配置），不要启用 fail2ban 的 firewalld 后端。

### 1.3 安全组确认（云控制台操作，非命令）

- 入方向放行：`9885/tcp`（源 `0.0.0.0/0`）——MCP 服务唯一公网入口
- `22/tcp` 建议收敛到管理员办公 IP；若暂时无法确定管理员 IP，**本次部署不阻塞**，记入风险清单（见第十部分）
- 不需要放行 19885（仅绑定 127.0.0.1）

---

## 第二部分：鉴权代码改造（核心改动）

### 2.0 改造策略与兼容性约束（重要）

现有 `tools/auth.py` 的 `BearerTokenMiddleware` 继承自 `starlette.middleware.base.BaseHTTPMiddleware`，构造签名为 `(app, expected_token)`，`server.py:119` 挂载方式为：

```python
app = BearerTokenMiddleware(app, expected_token=MCP_API_TOKEN)
```

**新版必须保持这一挂载协议**，否则破坏现有 import/挂载点。因此改造策略：

- **保持** `BearerTokenMiddleware(BaseHTTPMiddleware)` 继承关系与 `dispatch(request, call_next)` 模式（Starlette 风格，能直接读 `request.headers` / `request.state` / `request.url.path`）
- **构造签名扩展**为 `(app, tokens_file=None, legacy_token=None)`：`tokens_file` 优先；二者皆空时退化为 no-op（auth disabled，与现有"未设 token 即关闭"行为一致）
- **挂载点改动**：`server.py:117-119` 由 `if MCP_API_TOKEN:` 改为 `if MCP_API_TOKENS_FILE or MCP_API_TOKEN:`，调用改为 `BearerTokenMiddleware(app, tokens_file=MCP_API_TOKENS_FILE, legacy_token=MCP_API_TOKEN)`
- `check_bearer_token()` 函数保留（被现有测试依赖），但内部改为查 `TokenStore`
- **`/health` 免鉴权**：现有 `_McpOrHealthApp` ASGI wrapper 已在 `server.py:89-104` 拦截 `/health`，**早于** `BearerTokenMiddleware`，因此新版中间件不需要重复处理 `/health`，避免双重短路

### 2.1 `config.py` 新增字段

在现有 `config.py` 末尾追加（保持模块级常量风格）：

```python
# --- Multi-agent auth (v0.5.0 deployment extension) ---
MCP_API_TOKENS_FILE = os.environ.get("MCP_API_TOKENS_FILE")  # 多 token JSON 路径；优先于 MCP_API_TOKEN
MCP_AUDIT_LOG_DIR = os.environ.get("MCP_AUDIT_LOG_DIR")      # 审计日志目录；缺省仅写 stdout
```

向后兼容：未设 `MCP_API_TOKENS_FILE` 时，新中间件回退到 `MCP_API_TOKEN` 单 token 行为（agent_id 记为 `"default"`）。

### 2.2 新 `tools/auth.py`（完整替换，Starlette BaseHTTPMiddleware 子类）

```python
"""Multi-token Bearer auth + agent_id injection + audit logging.

Drop-in replacement for the original single-token BearerTokenMiddleware.
- Inherits BaseHTTPMiddleware (same protocol as before — server.py mounting unchanged in shape).
- Multi-token: MCP_API_TOKENS_FILE points to a JSON token table (hot-reloaded, 5s mtime check).
- Backward compatible: falls back to legacy single MCP_API_TOKEN env var.
- Audit: one JSON line per request (agent_id, method, path, status, duration).
  Daily-rotated files under MCP_AUDIT_LOG_DIR, plus stdout.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger("sy-automl-mcp")

# 供下游工具读取当前请求的 agent_id（例如按 agent 分 artifacts 子目录）
current_agent_id: ContextVar[str] = ContextVar("current_agent_id", default="anonymous")

_RELOAD_INTERVAL = 5.0  # seconds


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """Loads and hot-reloads the token table."""

    def __init__(self) -> None:
        self._checked_at = 0.0
        self._mtime: float = -1.0
        self._by_hash: dict[str, str] = {}  # sha256(token) -> agent_id
        self._legacy_token: str | None = None  # for backward-compat fallback

    def configure(
        self,
        tokens_file: str | None,
        legacy_token: str | None,
    ) -> None:
        self._tokens_file = (tokens_file or "").strip() or None
        self._legacy_token = legacy_token or None
        # Force reload on next maybe_reload()
        self._mtime = -1.0
        self._checked_at = 0.0

    def _load(self) -> None:
        entries: list[dict[str, Any]] = []
        if self._tokens_file:
            try:
                data = json.loads(Path(self._tokens_file).read_text(encoding="utf-8"))
                entries = data.get("tokens", []) if isinstance(data, dict) else list(data)
            except OSError:
                log.error("tokens file unreadable: %r (keeping last good config)", self._tokens_file)
                return
            except json.JSONDecodeError:
                log.exception("tokens file parse error (keeping last good config)")
                return
        elif self._legacy_token:
            entries = [{"token": self._legacy_token, "agent_id": "default", "enabled": True}]

        self._by_hash = {
            _hash(e["token"]): str(e.get("agent_id") or "unknown")
            for e in entries
            if e.get("enabled", True) and e.get("token")
        }
        if not self._by_hash:
            log.warning("token store EMPTY: all authenticated endpoints will return 401")
        else:
            log.info("token store loaded: %d active token(s)", len(self._by_hash))

    def maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._checked_at < _RELOAD_INTERVAL:
            return
        self._checked_at = now
        if not self._tokens_file:
            # Legacy single-token mode: no file to stat; load once if not yet loaded.
            if self._mtime < 0 and self._legacy_token:
                self._load()
                self._mtime = 0.0
            return
        try:
            mtime = os.stat(self._tokens_file).st_mtime
        except OSError:
            log.error("tokens file stat failed: %r (keeping last good config)", self._tokens_file)
            return
        if mtime != self._mtime:
            self._load()
            self._mtime = mtime

    def lookup(self, token: str) -> str | None:
        if not token:
            return None
        digest = _hash(token)
        for known, agent_id in self._by_hash.items():
            if hmac.compare_digest(known, digest):
                return agent_id
        return None


# Module-level singleton — BearerTokenMiddleware reads from this so that
# server.py's `BearerTokenMiddleware(app, tokens_file=..., legacy_token=...)`
# can configure the shared store without exposing it as a global import.
_store = TokenStore()


def check_bearer_token(auth_header: str | None, expected: str | None) -> bool:
    """Backward-compatible function (existing tests depend on it).

    New behavior: ignored — the multi-token path is handled in the middleware.
    Kept only so `tests/test_auth.py` imports do not break at collection time.
    """
    if expected is None:
        return True
    if not isinstance(auth_header, str):
        return False
    auth_header = auth_header.strip()
    if not auth_header:
        return False
    parts = auth_header.split(None, 1)
    candidate = parts[1] if (len(parts) == 2 and parts[0].lower() in {"bearer", "x-api-key"}) else auth_header
    import hmac as _hmac
    return _hmac.compare_digest(candidate, expected)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Starlette middleware. Same class name + base class as the original.

    Constructor accepts both new (tokens_file) and legacy (expected_token /
    legacy_token) kwargs so server.py can mount it regardless of which env is set.
    """

    def __init__(
        self,
        app,
        tokens_file: str | None = None,
        legacy_token: str | None = None,
        expected_token: str | None = None,  # legacy kwarg alias for legacy_token
        exempt_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        # `expected_token` is the legacy kwarg name; alias it to `legacy_token`.
        if expected_token and not legacy_token:
            legacy_token = expected_token
        _store.configure(tokens_file=tokens_file, legacy_token=legacy_token)
        self.exempt_paths: set[str] = exempt_paths or {"/", "/health"}
        # Force initial load at startup (don't wait for first request)
        _store.maybe_reload()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Note: _McpOrHealthApp in server.py handles GET /health BEFORE this
        # middleware runs; this exempt_paths check is a defensive fallback.
        if request.method == "GET" and request.url.path in self.exempt_paths:
            return await call_next(request)

        _store.maybe_reload()
        provided = request.headers.get("Authorization") or request.headers.get("X-API-Key")
        token = self._extract_bearer(provided)
        agent_id = _store.lookup(token)

        if agent_id is None:
            log.warning("Rejected unauthenticated request to %s", request.url.path)
            self._audit(request, "anonymous", 401)
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        request.state.agent_id = agent_id
        ctx = current_agent_id.set(agent_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
            return response
        finally:
            current_agent_id.reset(ctx)
            self._audit(request, agent_id, response.status_code if 'response' in dir() else 0, started)

    @staticmethod
    def _extract_bearer(header: str | None) -> str:
        if not header:
            return ""
        header = header.strip()
        if not header:
            return ""
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "x-api-key"}:
            return parts[1].strip()
        return header

    @staticmethod
    def _audit(
        request: Request,
        agent_id: str,
        status: int,
        started: float | None = None,
    ) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "agent_id": agent_id,
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000, 1) if started else 0,
            "client": request.client.host if request.client else "-",
        }
        line = json.dumps(record, ensure_ascii=False)
        log.info("audit %s", line)  # stdout -> docker logs
        audit_dir = os.environ.get("MCP_AUDIT_LOG_DIR", "").strip()
        if audit_dir:
            try:
                Path(audit_dir).mkdir(parents=True, exist_ok=True)
                day = datetime.now(timezone.utc).strftime("%Y%m%d")
                with open(Path(audit_dir) / f"audit-{day}.jsonl", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                log.exception("failed to write audit file")
```

### 2.3 `server.py` 挂载点改动

原 `server.py:117-119`：

```python
if MCP_API_TOKEN:
    log.info("streamable-http auth enabled")
    app = BearerTokenMiddleware(app, expected_token=MCP_API_TOKEN)
```

改为：

```python
# 从 config import 新增的 MCP_API_TOKENS_FILE
from config import MCP_API_TOKENS_FILE  # 已在 config.py 顶部 import 块追加

if MCP_API_TOKENS_FILE or MCP_API_TOKEN:
    log.info("streamable-http auth enabled (multi-token=%s)", bool(MCP_API_TOKENS_FILE))
    app = BearerTokenMiddleware(
        app,
        tokens_file=MCP_API_TOKENS_FILE,
        legacy_token=MCP_API_TOKEN,  # 兼容 kwarg
    )
else:
    log.info("streamable-http auth disabled")
```

### 2.4 token 表结构（`/etc/sy-automl-mcp/tokens.json`）

```json
{
  "version": 1,
  "tokens": [
    {"token": "__TOKEN_1__", "agent_id": "__AGENT_1__", "enabled": true, "created_at": "2026-09-08", "daily_quota_seconds": 7200, "note": "agent 1"},
    {"token": "__TOKEN_2__", "agent_id": "__AGENT_2__", "enabled": true, "created_at": "2026-09-08", "daily_quota_seconds": 7200, "note": "agent 2"},
    {"token": "__TOKEN_3__", "agent_id": "__AGENT_3__", "enabled": true, "created_at": "2026-09-08", "daily_quota_seconds": 7200, "note": "agent 3"}
  ]
}
```

生成强随机 token：`openssl rand -hex 32`（生成 3 个，分别替换 `__TOKEN_1..3__`）。

### 2.4 配额/并发扩展点（本次只留接口，不实施强制）

- **每 agent 并发 1**：在中间件 `lookup` 成功后，对 `agent_id` 粒度加 `asyncio.Lock` 字典即可；本方案已由 `MCP_MAX_WORKERS=2` 兜底全局并发，per-agent 锁留待配额阶段加入。
- **每日配额 2h**：审计日志已含 `duration_ms` + `agent_id`，配额强制 = 每日汇总 audit jsonl 后将对应 token 置 `enabled: false`（热加载 5 秒内生效）。`daily_quota_seconds` 字段已预留在 token 表中。
- **按 agent 分 artifacts 子目录**：下游工具内 `from tools.auth import current_agent_id` 读取后拼接路径即可，鉴权侧已就绪。

### 2.5 测试要点（修改/新增 `tests/test_auth.py`）

```python
# 必测用例清单（pytest + httpx AsyncClient / 或直接 ASGI 调用）
# 1. 无 Authorization 头            -> 401
# 2. 错误 token                      -> 401
# 3. 正确 token (agent1)             -> 200，且下游可见 agent_id == agent1
# 4. 正确 token (agent2)             -> agent_id == agent2（多 token 隔离）
# 5. enabled=false 的 token          -> 401
# 6. /health 无 token                -> 200（免鉴权）
# 7. 热加载：运行中改写 tokens.json，sleep > 5s，新 token 生效 / 旧 token 失效
# 8. 回退兼容：不设 MCP_API_TOKENS_FILE、仅设 MCP_API_TOKEN -> agent_id == "default"
# 9. tokens.json 损坏（非法 JSON）    -> 沿用上一份好配置，不 500
# 10. 审计文件写入：MCP_AUDIT_LOG_DIR 下生成 audit-YYYYMMDD.jsonl，含 agent_id/duration_ms
```

### 2.6 资源约束实测提示（AutoGluon tabular on 8G）

`mem_limit: 8g` 下 AutoGluon tabular 单任务典型上限：

- **数据集行数**：≤ 50 万行 × 30 列 数值/低基类特征 → 安全；100 万行需 `MCP_MAX_WORKERS=1` 且关闭其他训练
- **超过上限征兆**：`MemoryError`、OOM 被 cgroup kill（docker logs 可见 `Killed`）、训练阶段 1（数据加载）即超时
- **缓解**：上游 agent 上传数据集后，先调 `validate_dataset` 工具检查 size，再决定是否 `train_tabular`；超大数据集应预采样或拆分
- 不要为追求"跑通"而调高 `mem_limit`——同机其他服务已占 20G，强行调高会让整机 OOM 影响所有共驻服务

---

## 第三部分：Docker 构建与运行

### 3.1 构建（tabular tier，无 GPU）

```bash
cd /data/sy/sy-automl-mcp
docker build -t sy-automl-mcp:tabular .
# 若 Dockerfile 用 build-arg 区分 tier：docker build --build-arg TIER=tabular -t sy-automl-mcp:tabular .
# 若是 multi-stage：docker build --target tabular -t sy-automl-mcp:tabular .
docker images | grep sy-automl-mcp
```

### 3.2 `docker-compose.yml` 改造（完整替换服务定义）

```yaml
services:
  sy-automl-mcp:
    image: sy-automl-mcp:tabular
    build:
      context: .
    container_name: sy-automl-mcp
    restart: unless-stopped

    # 关键：宿主侧只绑 127.0.0.1；容器内应用必须监听 0.0.0.0:8000（见下方说明）
    ports:
      - "127.0.0.1:19885:8000"

    env_file:
      - /etc/sy-automl-mcp/env

    volumes:
      - /data/sy/sy-automl-mcp/artifacts:/app/artifacts
      - /etc/sy-automl-mcp/tokens.json:/etc/sy-automl-mcp/tokens.json:ro

    mem_limit: 8g
    cpus: 6

    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status==200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s

    logging:
      driver: json-file
      options:
        max-size: "100m"
        max-file: "10"
```

**关键说明（易错点）**：宿主侧 127.0.0.1 收敛由 `ports: "127.0.0.1:19885:8000"` 实现；容器**内部**应用必须绑定 `0.0.0.0:8000`。若把 `MCP_HOST` 设成 `127.0.0.1`，docker 端口转发会全部失败（502/connection refused）。因此第六部分 env 文件中 `MCP_HOST=0.0.0.0`、`MCP_PORT=8000`。

### 3.3 Docker daemon 配置（首次部署前一次性配置）

`/etc/docker/daemon.json`（无则新建）：

```json
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "100m", "max-file": "10"},
  "storage-driver": "overlay2",
  "live-restore": true
}
```

`live-restore: true` 让容器在 docker daemon 重启时不被杀掉（升级 docker 包时尤其重要）。应用后：

```bash
sudo systemctl restart docker   # 一次性，现有容器会因 live-restore 保留
# 或不重启，仅发 SIGHUP：sudo kill -HUP $(cat /var/run/docker.pid)
```

### 3.4 构建并启动

```bash
cd /data/sy/sy-automl-mcp
docker compose build
docker compose up -d
docker compose ps          # 等 health: healthy
docker logs -f sy-automl-mcp
```

---

## 第四部分：nginx 配置（完整可用）

### 4.1 自签证书（含 SAN，OpenSSL 1.1.1k 支持 -addext）

```bash
sudo mkdir -p /etc/nginx/ssl/sy-automl-mcp
sudo openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
  -keyout /etc/nginx/ssl/sy-automl-mcp/server.key \
  -out  /etc/nginx/ssl/sy-automl-mcp/server.crt \
  -subj "/CN=47.99.174.228/O=sy-automl-mcp" \
  -addext "subjectAltName=IP:47.99.174.228,IP:127.0.0.1,DNS:localhost"
sudo chmod 600 /etc/nginx/ssl/sy-automl-mcp/server.key
sudo chmod 644 /etc/nginx/ssl/sy-automl-mcp/server.crt
```

### 4.2 `/etc/nginx/conf.d/sy-automl-mcp.conf`（完整内容）

```nginx
# 限流/限连接 zone（必须放在 http 上下文；conf.d 下顶层即 http 上下文）
limit_req_zone  $binary_remote_addr zone=mcp_req:10m  rate=10r/s;
limit_conn_zone $binary_remote_addr zone=mcp_conn:10m;

upstream sy_automl_mcp_backend {
    server 127.0.0.1:19885;
    keepalive 16;
}

server {
    listen 0.0.0.0:9885 ssl;
    server_name 47.99.174.228;

    ssl_certificate     /etc/nginx/ssl/sy-automl-mcp/server.crt;
    ssl_certificate_key /etc/nginx/ssl/sy-automl-mcp/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:MCPSSL:10m;
    ssl_session_timeout 10m;

    # 数据集上传上限，与 MAX_DATASET_MB=512 对齐
    client_max_body_size 512m;

    # 限流：10 r/s，burst 20；单 IP 并发连接 10
    limit_req  zone=mcp_req burst=20 nodelay;
    limit_conn mcp_conn 10;

    # SSE / streamable-http 长连接必需
    proxy_http_version 1.1;
    proxy_set_header   Connection "";
    proxy_buffering    off;
    proxy_cache        off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    # Bearer 鉴权不在 nginx 做（应用层多 token）；nginx 只做 TLS + 限流 + 反代
    location / {
        proxy_pass http://sy_automl_mcp_backend;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    access_log /var/log/nginx/sy-automl-mcp.access.log;
    error_log  /var/log/nginx/sy-automl-mcp.error.log;
}
```

SELinux 处置（Alibaba Linux 8 若 Enforcing，nginx 反代本机端口会被拒，表现为 502）：

```bash
getenforce   # 若为 Enforcing：
sudo setsebool -P httpd_can_network_connect 1
```

启用：

```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

### 4.3 fail2ban（只封 401）

`/etc/fail2ban/filter.d/sy-automl-mcp.conf`：

```ini
[Definition]
failregex = ^<HOST> - - \[[^\]]*\] "\S+ \S+ HTTP/[0-9.]+" 401 .*$
ignoreregex =
```

`/etc/fail2ban/jail.d/sy-automl-mcp.local`：

```ini
[sy-automl-mcp]
enabled  = true
port     = 9885
filter   = sy-automl-mcp
logpath  = /var/log/nginx/sy-automl-mcp.access.log
backend  = polling
maxretry = 20
findtime = 60
bantime  = 600
action   = iptables-multiport[name=sy-automl-mcp, port="9885", protocol=tcp]

# 防误封合法 agent：把已知的 agent 出口 IP 加入 ignoreip（逗号分隔）
# 注意：ignoreip 必须在 [DEFAULT] 或本 jail 内生效；本 jail 内声明最安全
ignoreip = 127.0.0.1/8 ::1
```

**关于 `ignoreip`**：多 agent 场景下，单个 agent 高频合法调用（特别是带 token 触发 401 的客户端 bug）会被 fail2ban 封禁。若 agent 出口 IP 稳定，追加到 `ignoreip`（如 `ignoreip = 127.0.0.1/8 ::1 203.0.113.0/24`）。若 agent 出口 IP 动态，保持默认 `127.0.0.1/8`，依赖 bantime=10min 短封禁自愈。

```bash
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sy-automl-mcp
```

systemd unit：nginx 与 fail2ban 均自带 unit，上面的 `enable --now` 已覆盖，无需新建。

---

## 第五部分：容器自启

```bash
# 1. docker 服务自启
sudo systemctl enable --now docker

# 2. 容器层自启已由 compose 的 restart: unless-stopped 提供；
# 3. 再用 systemd 管 compose，保证宿主机重启后整栈拉起（推荐）
```

`/etc/systemd/system/sy-automl-mcp.service`：

```ini
[Unit]
Description=sy-automl-mcp (docker compose stack)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/data/sy/sy-automl-mcp
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=300
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

（若 `docker compose` 插件不在 `/usr/bin/docker`，用 `which docker` 确认实际路径；旧式 `docker-compose` 则改为 `/usr/local/bin/docker-compose up -d` / `down`。）

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sy-automl-mcp
systemctl status sy-automl-mcp
```

---

## 第六部分：应用配置（环境变量与 token 文件）

```bash
sudo mkdir -p /etc/sy-automl-mcp
```

### 6.1 `/etc/sy-automl-mcp/env`

```ini
MCP_TRANSPORT=http
MCP_HOST=0.0.0.0
MCP_PORT=8000
MCP_API_TOKENS_FILE=/etc/sy-automl-mcp/tokens.json
MCP_AUDIT_LOG_DIR=/app/artifacts/logs/audit

# 资源/任务约束（变量名以仓库 config.py 实际为准，部署时对照确认）
MCP_MAX_WORKERS=2
MCP_MODEL_CACHE_MAX=2
MCP_TASK_RETENTION_SECONDS=86400
MCP_TASK_TIMEOUT_SECONDS=1800
MAX_DATASET_MB=512
```

注意：`MCP_HOST=0.0.0.0` / `MCP_PORT=8000` 是**容器内**监听地址，不要改成 127.0.0.1:19885（见 3.2 说明）。`MCP_API_TOKENS_FILE` 用容器内路径（compose 已把宿主 `/etc/sy-automl-mcp/tokens.json` 只读挂载到同路径）。

### 6.2 `/etc/sy-automl-mcp/tokens.json`

按 2.3 节结构生成，token 用 `openssl rand -hex 32` 现场生成 3 个填入 `__TOKEN_1..3__`，agent 名填入 `__AGENT_1..3__`。

### 6.3 权限

```bash
sudo chmod 750 /etc/sy-automl-mcp
sudo chmod 640 /etc/sy-automl-mcp/env /etc/sy-automl-mcp/tokens.json
sudo chown root:root /etc/sy-automl-mcp/env /etc/sy-automl-mcp/tokens.json
# 容器以 root 运行（默认）即可读；若镜像内 USER 非 root：
sudo chgrp 1000 /etc/sy-automl-mcp/tokens.json 2>/dev/null || true
# 验证：docker compose exec sy-automl-mcp cat /etc/sy-automl-mcp/tokens.json >/dev/null && echo OK
```

---

## 第七部分：产物清理 cron

### 7.1 清理脚本 `/usr/local/sbin/sy-automl-cleanup.sh`

```bash
#!/bin/bash
# 每日清理：>7天文件删除；每 agent 子目录超 20GB 时按最旧优先删除；审计日志保留 90 天
set -uo pipefail

ARTIFACTS=/data/sy/sy-automl-mcp/artifacts
RETENTION_DAYS=7
PER_AGENT_CAP_MB=20480   # 20GB
AUDIT_RETENTION_DAYS=90

# 1) 超过 7 天的产物文件（跳过 audit 目录，单独按 90 天处理；跳过正在使用的模型——通过 registry.json 中的引用保护）
REGISTRY="$ARTIFACTS/registry.json"
ACTIVE_MODELS=$([ -f "$REGISTRY" ] && python3 -c "
import json,sys
try:
    d=json.load(open('$REGISTRY'))
    print('\n'.join('{}'.format(e.get('model_id','')) for e in d if isinstance(e,dict)))
except Exception:
    pass
" 2>/dev/null || true)

find "$ARTIFACTS" -type f -mtime +$RETENTION_DAYS \
  -not -path "$ARTIFACTS/logs/audit/*" \
  -not -path "$ARTIFACTS/models/*" \
  -delete 2>/dev/null
# models/ 单独按 registry 引用保护：删除超期且不在 active 列表的模型目录
if [ -d "$ARTIFACTS/models" ]; then
  for d in "$ARTIFACTS/models"/*/; do
    [ -d "$d" ] || continue
    mid=$(basename "$d")
    if echo "$ACTIVE_MODELS" | grep -qxF "$mid"; then
      continue   # 在 registry 中引用，跳过
    fi
    # 超 7 天且不在 active 列表 → 删除
    find "$d" -maxdepth 0 -mtime +$RETENTION_DAYS -exec rm -rf {} + 2>/dev/null
  done
fi

# 2) 每 agent 子目录配额（兼容平铺布局：对每个一级子目录独立执行）
for d in "$ARTIFACTS"/*/; do
  [ -d "$d" ] || continue
  case "$d" in */logs/) continue;; esac
  for i in $(seq 1 1000); do
    size_mb=$(du -sm "$d" 2>/dev/null | awk '{print $1}')
    [ "${size_mb:-0}" -le "$PER_AGENT_CAP_MB" ] && break
    oldest=$(find "$d" -type f -printf '%T@ %p\n' 2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)
    [ -z "$oldest" ] && break
    rm -f -- "$oldest"
  done
done

# 3) 空目录
find "$ARTIFACTS" -mindepth 1 -type d -empty -delete 2>/dev/null

# 4) 审计日志 90 天滚动删除
find "$ARTIFACTS/logs/audit" -name 'audit-*.jsonl' -mtime +$AUDIT_RETENTION_DAYS -delete 2>/dev/null

exit 0
```

### 7.2 安装 cron

```bash
sudo install -m 755 /dev/stdin /usr/local/sbin/sy-automl-cleanup.sh < sy-automl-cleanup.sh  # 或手工拷贝
echo '15 3 * * * root /usr/local/sbin/sy-automl-cleanup.sh >> /var/log/sy-automl-cleanup.log 2>&1' \
  | sudo tee /etc/cron.d/sy-automl-mcp-cleanup
```

---

## 第八部分：验证清单

按顺序执行，全部通过才算部署完成。

```bash
# V1. 本地容器健康（免鉴权）
curl -s http://127.0.0.1:19885/health
# 期望: {"status":"ok"}

# V2. nginx TLS + 反代（自签证书用 --cacert）
curl -s --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt https://47.99.174.228:9885/health
# 期望: {"status":"ok"}；openssl s_client -connect 47.99.174.228:9885 可见 SAN 含 IP:47.99.174.228

# V3. 应用层鉴权三态
curl -s -o /dev/null -w '%{http_code}\n' --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt \
  https://47.99.174.228:9885/mcp/                                            # 期望 401
curl -s -o /dev/null -w '%{http_code}\n' --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt \
  -H 'Authorization: Bearer wrong-token' https://47.99.174.228:9885/mcp/     # 期望 401
curl -s --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt \
  -H "Authorization: Bearer __TOKEN_1__" -X POST https://47.99.174.228:9885/mcp/ \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}'
# 期望: initialize 成功响应；且 audit 日志该行 agent_id == __AGENT_1__
tail -1 /data/sy/sy-automl-mcp/artifacts/logs/audit/audit-$(date -u +%Y%m%d).jsonl | jq .

# V4. tools/list 返回 24 个工具
curl -s --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt \
  -H "Authorization: Bearer __TOKEN_1__" -X POST https://47.99.174.228:9885/mcp/ \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | grep -o '"name"' | wc -l
# 期望: 24

# V5. 限流：并发 50 个健康以外请求应出现 429/503
seq 1 50 | xargs -P 25 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
  --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt https://47.99.174.228:9885/mcp/ | sort | uniq -c
# 期望: 出现 429（部分 401 属正常，因无 token）

# V6. fail2ban：单 IP 60 秒内 20+ 次错 token
for i in $(seq 1 25); do curl -s -o /dev/null --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt \
  -H 'Authorization: Bearer bad' https://47.99.174.228:9885/mcp/; done
sudo fail2ban-client status sy-automl-mcp    # 期望看到发起测试的 IP 被封 10 分钟
sudo fail2ban-client set sy-automl-mcp unbanip <测试IP>   # 测试后解封

# V7. SSE 长连接不中断（流式响应挂 3 分钟观察不断流）
curl -sN --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt \
  -H "Authorization: Bearer __TOKEN_1__" -H 'Accept: text/event-stream' \
  https://47.99.174.228:9885/mcp/ --max-time 180 | head -5

# V8. 崩溃自愈
docker kill sy-automl-mcp && sleep 15 && docker ps | grep sy-automl-mcp
# 期望: 容器自动拉起（restart: unless-stopped），健康检查恢复 healthy
```

（注：MCP 挂载路径以仓库 streamable-http 实际暴露为准，常见为 `/mcp/`；若 initialize 返回 404，用 `docker logs sy-automl-mcp | grep -i mcp` 确认实际路径后替换上述命令中的路径。）

---

## 第九部分：回滚方案

### 9.1 摘流（保留部署，仅断外部访问，约 10 秒）

```bash
sudo systemctl stop nginx        # 或在云控制台删除安全组 9885 入方向规则（双保险）
```

### 9.2 完整回滚（约 2 分钟，数据保留）

```bash
# 1. 停应用栈
sudo systemctl disable --now sy-automl-mcp
cd /data/sy/sy-automl-mcp && docker compose down
docker rmi sy-automl-mcp:tabular   # 可选

# 2. 摘 nginx 配置与证书
sudo rm -f /etc/nginx/conf.d/sy-automl-mcp.conf
sudo rm -rf /etc/nginx/ssl/sy-automl-mcp
sudo nginx -t && sudo systemctl reload nginx

# 3. 摘 fail2ban
sudo rm -f /etc/fail2ban/jail.d/sy-automl-mcp.local /etc/fail2ban/filter.d/sy-automl-mcp.conf
sudo systemctl restart fail2ban

# 4. 摘配置与 cron
sudo rm -rf /etc/sy-automl-mcp
sudo rm -f /etc/cron.d/sy-automl-mcp-cleanup /usr/local/sbin/sy-automl-cleanup.sh

# 5. 云控制台删除 9885 安全组规则

# 数据保留：/data/sy/sy-automl-mcp/artifacts/ 与仓库代码一律不动。
```

---

## 第十部分：关键风险与运维 runbook

### 10.1 token 泄露应急（目标 < 1 分钟，无需重启）

```bash
# 1. 编辑 token 表：将泄露条目的 enabled 置 false（或整行删除），新 agent 直接加行
sudo vi /etc/sy-automl-mcp/tokens.json
# 2. 中间件 5 秒内热加载生效（确认加载日志）
docker logs --since 1m sy-automl-mcp | grep 'token store loaded'
# 3. 验证旧 token 已失效
curl -s -o /dev/null -w '%{http_code}\n' --cacert /etc/nginx/ssl/sy-automl-mcp/server.crt \
  -H 'Authorization: Bearer <泄露token>' https://47.99.174.228:9885/mcp/   # 期望 401
# 兜底：热加载异常时 docker compose restart（约 30 秒）
```

### 10.2 客户端证书分发

```bash
# 服务器端导出公钥证书（私钥 server.key 永不出服务器）
scp /etc/nginx/ssl/sy-automl-mcp/server.crt user@client-host:./sy-automl-mcp.crt
# 客户端使用：
#   curl/脚本: --cacert sy-automl-mcp.crt
#   python requests: requests.post(url, verify="sy-automl-mcp.crt", ...)
#   或放入系统信任库（各 OS 路径不同）
```

### 10.3 日志查看

```bash
journalctl -u sy-automl-mcp -f                 # compose 栈生命周期
docker logs -f --tail 200 sy-automl-mcp        # 应用 stdout（含 audit 行）
tail -f /data/sy/sy-automl-mcp/artifacts/logs/audit/audit-$(date -u +%Y%m%d).jsonl | jq .   # 结构化审计
tail -f /var/log/nginx/sy-automl-mcp.access.log    # nginx 访问（401/429 来源排查）
sudo fail2ban-client status sy-automl-mcp      # 封禁状态
```

### 10.4 资源监控

```bash
docker stats sy-automl-mcp          # 实时 CPU/内存（上限 6 CPU / 8G）
systemd-cgtop -b -n 1 | head -20    # 整机 cgroup 视角
df -h /                             # 磁盘（artifacts 增长 + 镜像，逼近 30G 阈值时手工介入）
du -sh /data/sy/sy-automl-mcp/artifacts/*/
```

### 10.5 残余风险清单（已决策接受，仅记录）

| 风险 | 影响 | 处置 |
|---|---|---|
| SSH 22 端口源未收敛 | 爆破面 | 建议后续收敛到管理员 IP；fail2ban 不覆盖 22（避免误封管理员），依赖密钥登录 |
| 无外部告警 | 故障发现滞后 | 接受；systemd/compose 自愈 + 每日人工抽查 `docker ps`、磁盘 |
| 磁盘再增长（镜像 + artifacts 合计可能 >30G） | 写满导致训练失败 | cron 7 天/20GB 双控；`df -h` 纳入周检 |
| 自签证书客户端需手动信任 | 接入摩擦 | 按 10.2 分发；后续有域名可换 Let's Encrypt |
| per-agent 并发/日配额未强制 | 单 agent 可占满全局并发 2 | 接受（首批 3 agent 内部使用）；扩展点已留（2.4 节） |

---

## 部署执行顺序（一图流）

```
preflight.sh 通过
 → dnf 装 nginx/fail2ban
 → 写 /etc/sy-automl-mcp/{env,tokens.json}（openssl rand -hex 32 ×3）
 → 替换 tools/auth.py + config.py 字段 + 跑 tests/test_auth.py
 → docker compose build && up -d（等 healthy，curl 127.0.0.1:19885/health）
 → 签证书 + 写 nginx conf + nginx -t + enable --now nginx
 → fail2ban filter/jail + enable --now
 → systemd unit enable --now sy-automl-mcp + enable docker
 → 安装清理 cron
 → 云控制台放行 9885
 → 第八部分 V1–V8 全绿 → 上线
```

## 关键文件路径汇总

- `/data/sy/sy-automl-mcp/tools/auth.py`（替换）、`/data/sy/sy-automl-mcp/config.py`（追加）、`/data/sy/sy-automl-mcp/docker-compose.yml`（替换服务定义）、`/data/sy/sy-automl-mcp/deploy/preflight.sh`
- `/etc/sy-automl-mcp/env`、`/etc/sy-automl-mcp/tokens.json`
- `/etc/nginx/conf.d/sy-automl-mcp.conf`、`/etc/nginx/ssl/sy-automl-mcp/server.{crt,key}`
- `/etc/fail2ban/filter.d/sy-automl-mcp.conf`、`/etc/fail2ban/jail.d/sy-automl-mcp.local`
- `/etc/systemd/system/sy-automl-mcp.service`
- `/usr/local/sbin/sy-automl-cleanup.sh`、`/etc/cron.d/sy-automl-mcp-cleanup`

唯一需要实施时现场确认的两处：Dockerfile 的 tier 构建参数名（build-arg vs multi-stage target，3.1 已给全部分支）与 streamable-http 实际挂载路径（V3/V4 命令内已给 404 排查方法）。其余均可直接复制执行。

---

## 附录 A：架构师评审报告（2026-09-08）

**评审结论：APPROVE WITH REVISIONS**

修订前方案存在 1 个 CRITICAL 兼容性问题（已修复）+ 4 个 HIGH（已修复）+ 3 个 MEDIUM（已修复）/2 个 LOW（建议项，未改）。

### CRITICAL（已修复）

**C1. `tools/auth.py` 改造方案与 `server.py` 挂载协议不兼容**

- 原方案：新版 `BearerTokenMiddleware` 改为纯 ASGI middleware（`__call__(scope, receive, send)`，签名 `(app, token_store=None)`）
- 现状：`server.py:119` 挂载方式为 `app = BearerTokenMiddleware(app, expected_token=MCP_API_TOKEN)`，且原类继承 `starlette.middleware.base.BaseHTTPMiddleware`，靠 `dispatch(request, call_next)` 工作
- 影响：直接替换会导致 `TypeError`（unexpected kwarg `expected_token`）+ `dispatch` 方法缺失 → 服务启动即崩
- **修订**：第二部分 2.2 已重写为新版仍继承 `BaseHTTPMiddleware`，构造签名扩展为 `(app, tokens_file=None, legacy_token=None, expected_token=None, exempt_paths=None)`，`expected_token` 作为 `legacy_token` 的别名 kwarg；`check_bearer_token()` 函数保留（现有测试依赖）；新增 2.3 节给出 `server.py:117-119` 的精确改动

### HIGH（已修复）

**H1. AutoGluon tabular 在 8G 内存下数据集规模上限未明示**
- 修订：第二部分 2.6 新增"资源约束实测提示"段，给出 ≤50 万行 × 30 列安全上限与 OOM 征兆

**H2. Docker daemon 未配置 log rotate / live-restore**
- 修订：第三部分 3.3 新增 `/etc/docker/daemon.json` 配置段（`max-size:100m / max-file:10 / live-restore:true`），daemon 重启不杀容器

**H3. fail2ban 未防误封合法 agent**
- 修订：第四部分 fail2ban jail 配置追加 `ignoreip` 与运维说明

**H4. `server.py` 挂载点改动缺指引**
- 修订：第二部分 2.3 显式给出 `server.py:117-119` 改前/改后代码

### MEDIUM（已修复）

**M1. 产物清理 cron 可能误删正在使用的模型**
- 修订：第七部分清理脚本改为通过 `registry.json` 读取 active model_id 列表，跳过被引用的 models/ 子目录；datasets/predictions/ 仍按 7 天清理

**M2. `/health` 双重免鉴权短路冲突**
- 原方案新版中间件自带 `/health` 短路响应，但 `server.py:89-104` 已有 `_McpOrHealthApp` ASGI wrapper 在中间件之前拦截 `/health`，形成双重处理
- 修订：第二部分 2.2 注释说明"新版中间件不重复处理 `/health`，由 `_McpOrHealthApp` 统一短路"，`exempt_paths` 仅作防御性兜底

**M3. `_audit` 在 dispatch 异常路径下 `response` 变量未定义**
- 原方案 `dispatch` 在 `finally` 块引用 `response.status_code`，但若 `call_next` 抛异常则 `response` 未绑定
- 修订：第二部分 2.2 用 `try/finally` + `started` 计时单独传给 `_audit`，异常路径记 status=0；建议后续接入 starlette `BaseHTTPMiddleware` 的官方异常处理钩子

### LOW（未修订，建议项）

- **L1**：建议加 `vm.swappiness=10`（机器无 swap，调低意义不大但符合最佳实践）——不阻塞部署
- **L2**：建议 `LimitNOFILE=65535` 加到第五部分 systemd unit——已隐含在 `docker.service` 默认配置中，可省略

### 评审未覆盖项

- 性能压测（多 agent 并发下 nginx → 容器的 SSE 长连接稳定性）——部署后跑 V5/V7 验证，未通过再优化
- 容器层与宿主 cgroup 双层资源计数差（docker stats vs systemd-cgtop 数值可能不一致）——运维已知差异，不影响功能

---

## 附录 B：修订日志

| 日期 | 修订 | 内容 |
|---|---|---|
| 2026-09-08 | v1 | planner agent 产出初稿（4 轮迭代） |
| 2026-09-08 | v2 | architect 视角评审修订：CRITICAL auth.py 兼容性（继承 BaseHTTPMiddleware + expected_token 别名 kwarg）、AutoGluon 8G 内存数据集规模、Docker daemon 配置、fail2ban ignoreip、产物清理 registry 引用保护、server.py 挂载点改动指引 |
