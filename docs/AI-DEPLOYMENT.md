# AI Tool Gateway 部署指南

本文件说明 **hohu-admin AI Tool Gateway** 的生产部署流程，包括运行模式、权限初始化、Provider 出站控制、升级和回滚要求。

### 2026-09-18：Agent 管理升级注意

前后端需一起升级：Agent 管理改为「AI 助手 → Agent 管理」，复用当前登录；登录页入口和二次密码表单移除。更新后重新获取用户信息与菜单；仅默认系统范围的启用 `R_SUPER` 可访问，账号名不授予权限。旧 `/platform` 浏览器地址兼容跳转，旧平台 token 不再访问 Agent 接口。

执行 `alembic upgrade head` 应用当前 head `8946c48f5315`（升级边界见 [数据库迁移指南](DATABASE-MIGRATIONS.md)）：仅将内置 R_SUPER 的原标准名称按范围更新为“系统超级管理员”/“租户管理员”，保留角色 ID、成员、权限和自定义名称。升级前确认初始管理账号实际绑定启用的系统角色；不得依赖名为 admin 的无角色账号。其他平台维护 API 的认证尚未迁移，参见[安全指南](AI-SECURITY.md)。

**所有 AI 相关生产部署前必读**。普通业务模块部署见项目根 `README.md` / `Dockerfile`。

---

## 1. 部署前置

### 1.1 环境要求

| 组件 | 版本 | 必填 |
|---|---|---|
| Python | ≥ 3.12 | ✅ |
| PostgreSQL | ≥ 14（含 `JSONB` / `TIMESTAMP WITHOUT TIME ZONE`） | ✅ |
| Redis | ≥ 6（含 `HASH` / `EXPIRE` / `SCAN`） | ✅ |
| uv | ≥ 0.11 | ✅ |
| LLM Provider | OpenAI / Anthropic / Doubao 等兼容 OpenAI API 的厂商 | ✅ |

### 1.2 强制约束（spec §8.4 + 修订 S-6 + §8.4.1 v1.5+）

**两种部署模式**：

**模式 A — `AI_HITL_MODE=memory`（默认，单进程）**：
- **单 worker 进程**（不是单 pod）：`WEB_CONCURRENCY=1` + `uvicorn --workers 1` 或 gunicorn `--workers 1`。进程内 `asyncio.Event` 实现 HITL 唤醒，多 worker 下会静默失效。
- **修订 S-6 启动实测**：`AI_REQUIRE_SINGLE_WORKER=True`（默认）时，lifespan 用 Redis SADD 实测活跃 worker 数 > 1 则 `RuntimeError` 阻断启动。env var WEB_CONCURRENCY 不可信（uvicorn --workers 4 不经 gunicorn 时各 worker lifespan 独立运行，都通过 env var 检查）。
- **禁止 Docker/k8s 多 pod 部署**：多 pod = 多独立 `_pending` dict，HITL wake 必失配。
- **测试环境豁免**：单测 / 集成测试可设 `AI_REQUIRE_SINGLE_WORKER=False` 跳过此检查。

**模式 B — `AI_HITL_MODE=redis_pubsub`（v1.5+，2026-07-13 落地，spec §8.4.1 / SR-7）**：
- **可水平扩展**：多 worker / 多 pod / k8s 部署均可。wake 走 Redis pub/sub 跨进程通知，进程间零状态共享。
- **Redis 连接池大小要求**：`REDIS_POOL_SIZE ≥ max_concurrent_hitl_streams + 10`（每个 hang 占一个 pubsub 连接；同时挂起的流通常 < 用户数 × 平均并发率）。
- **Redis 稳定性是硬约束**：pubsub 消息丢失 = 挂起流等满 5min TTL 超时（有 `pending.wake_action` 字段兜底，但仅防 subscribe 前的 race；订阅期间的 Redis 断连仍会丢消息）。
- **启动检查放开**：lifespan 跳过单 worker assertion（`AI_REQUIRE_SINGLE_WORKER` 仅 memory 模式生效）。

**模式切换零代码改动**：`executor.py` / `api/confirm.py` 等调用方完全不变，mode 分支在 `HitlManager` 内部。

### 1.3 资源基线（参考）

| 资源 | 小型部署（< 100 用户） | 中等规模（1k 用户） |
|---|---|---|
| CPU | 2 核 | 4 核 |
| 内存 | 2 GB | 4 GB |
| PostgreSQL | 1 GB disk | 10 GB disk |
| Redis | 64 MB | 256 MB |
| LLM API | 按调用量付费 | 按调用量付费 |

---

## 2. 配置项（`.env`）

完整配置项见 `app/core/config.py`。AI 相关关键项：

```bash
# ===== 模块开关（spec §11.5）=====
# 整个 AI 模块全局开关。默认 true；false = AI 业务组件不初始化，/ai/** 统一 503 AI_MODULE_DISABLED
AI_MODULE_ENABLED=true

# ===== HITL 模式（spec §8.4 / §8.4.1）=====
# memory（默认，单进程）：进程内 asyncio.Event；强制 WEB_CONCURRENCY=1，禁止多 pod
# redis_pubsub（v1.5+）：Redis pub/sub 跨 worker；可水平扩展，多 pod / 多 worker 均可
WEB_CONCURRENCY=1              # memory 模式强制=1；redis_pubsub 模式可放开
AI_HITL_MODE=memory            # 切到 redis_pubsub 时 WEB_CONCURRENCY 可放开

# ===== HITL 配置 =====
AI_HITL_PENDING_TTL_SEC=300    # 5 分钟，confirmation 过期时间
AI_HITL_ARGS_MAX_BYTES=4096    # 4KB，防恶意 user 撑爆 Redis

# ===== LLM Provider（任选其一）=====
AI_DEFAULT_MODEL=openai:gpt-4o
AI_OPENAI_API_KEY=sk-xxx
AI_OPENAI_BASE_URL=https://api.openai.com/v1
# 或
AI_ANTHROPIC_API_KEY=sk-ant-xxx

# ===== Provider hardened egress（部署方配置，API payload 不可扩张）=====
# 官方 OpenAI/Anthropic/DeepSeek origin 已内置；自定义 Provider 加精确 origin。
AI_PROVIDER_EGRESS_ALLOWED_ORIGINS=https://models.example.com:443
# 私网/本地模型还必须命中显式 CIDR；不要用宽泛网段替代精确授权。
AI_PROVIDER_EGRESS_ALLOWED_CIDRS=10.20.30.40/32
AI_PROVIDER_EGRESS_CONNECT_TIMEOUT_SEC=5
AI_PROVIDER_EGRESS_READ_TIMEOUT_SEC=30
AI_PROVIDER_EGRESS_TOTAL_TIMEOUT_SEC=60
AI_PROVIDER_EGRESS_MAX_RESPONSE_BYTES=2097152
AI_PROVIDER_EGRESS_MAX_CONCURRENCY=20
AI_PROVIDER_EGRESS_MAX_RETRIES=1

# ===== LLM 调用参数 =====
# Generation options are configured per model in the Provider page.
# Omitted options use the model/provider defaults.

# ===== Redis / DB（继承项目主配置）=====
REDIS_URL=redis://127.0.0.1:6379/0
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
SECRET_KEY=<strong-random>      # JWT 签名，必须强随机
```

### 生产部署 checklist（spec §11.5 §6）

- [ ] `AI_MODULE_ENABLED=true`；若因紧急熔断设为 false，已验证 `/ai/**` 统一 503 + `AI_MODULE_DISABLED` 且 AI 业务依赖未初始化
- [ ] HITL 模式选择：
  - [ ] `AI_HITL_MODE=memory`：`WEB_CONCURRENCY=1`（强制）
  - [ ] `AI_HITL_MODE=redis_pubsub`：可放开 `WEB_CONCURRENCY`；验证 Redis 连接池大小 ≥ `max_concurrent_hitl_streams + 10`
- [ ] `SECRET_KEY` 强随机值（**不 reuse dev 默认**）
- [ ] 数据库账号最小权限（不用 superuser）
- [ ] Redis 启用 AUTH + 网络隔离
- [ ] LLM API Key 加密存储（Fernet，已实现）
- [ ] `python -m tools.ops.audit_ai_provider_egress` 无未处置 finding；存量 `EGRESS_POLICY_BLOCKED` 未被自动放行或翻转 `enabled`
- [ ] 已配置六个 `AI_E2E_*` 变量并通过真实 Provider 的 `pnpm e2e:provider`；不得用确定性 route fixture 或缺凭据 skip 代替
- [ ] HTTPS 全链路（Nginx / Caddy TLS 终止）
- [ ] Redis 共享请求限流可用，阈值在「系统设置 → 访问保护」配置
- [ ] 审计日志保留 ≥ 90 天（`ai_operation_log` / `sys_login_log` / `sys_operation_log`）
- [ ] ERROR 日志告警接入

---

## 3. 数据库迁移 + seed

### 3.1 创建迁移

```bash
# 新增 AI 相关字段（trace_id / agent_code / is_security_event 等）
alembic upgrade head
```

关键迁移：`c7d8e9f0a1b2_add_governed_ai_management_schema.py`（合并 AI Gateway、Supervisor、HITL、授权 lineage、Trace 与会话软删除 schema）。

`v0.1.4` 的不可变迁移边界为 `bf244f9a8b76`。升级前必须检查 `alembic current`：生产数据库只允许从该 revision 或其祖先升级；若测试数据库记录了已压缩移除的 revision，必须清理重建，禁止直接 `stamp` 到新 head。

### 3.2 CLI 自动初始化与升级

用户执行 `hohu deploy`（源码更新使用 `hohu deploy upgrade`），CLI 的 migrator
在 Alembic 成功后统一调用 `python -m scripts.init_db`，自动判断首次安装或已有部署。
不再分别执行菜单、配置、Agent 与 Prompt 脚本，也不再使用 `--init`。

首次管理员密码由 CLI 生成并保存在部署 `.env` 的 `HOHU_ADMIN_PASSWORD`。
种子在一个事务中执行，失败回滚；重复执行保留密码、角色授权、自定义配置与 Prompt，
不会清库。菜单定义共用 System 模块的静态目录，hosted 租户仅同步既定能力集合。

维护人员可单独执行只读部署检查：

```bash
uv run python -m tools.ops.audit_ai_provider_egress
BUILD_SHA=<new-build-sha> uv run python -m tools.ops.audit_data_scope_union \
  --output /protected/phase2-scope-preflight.json
```

完整目录与契约见 [SCRIPTS-DEPLOYMENT.md](SCRIPTS-DEPLOYMENT.md)。

### 3.3 验证

```bash
# 启动后 lifespan 会自动跑 ToolRegistry.validate_on_startup
# 校验 agent_code + permission_code 在 DB 存在，失败仅日志告警不阻断
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

启动日志应看到 `AI Tool Registry 启动校验通过`。失败会 `ERROR app.ai: AI Tool Registry 启动校验失败: ...`。

---

## 4. 启动检查（lifespan）

`app/main.py::lifespan` 启动时按顺序执行：

1. **单 worker assertion**（spec §8.4，仅 memory 模式）：
   ```python
   if settings.AI_HITL_MODE == "memory" and settings.AI_REQUIRE_SINGLE_WORKER:
       worker_count = await _detect_actual_worker_count()
       if worker_count > 1:
           raise RuntimeError(...)  # 阻断启动
   ```
   `redis_pubsub` 模式跳过此检查（spec §8.4.1 v1.5+）。

2. **加载内置 tools**：`load_builtin_tools()` 触发各业务模块 `@ai_tool` 装饰器注册到 `ToolRegistry`

3. **启动校验**：`ToolRegistry.get().validate_on_startup(db)`
   - 校验每个 tool 的 `agent_code` 在 `ai_agent` 表存在
   - 校验每个 tool 的 `required_perms` 在 `sys_menu` 表存在
   - 校验 `dry_run_supported=True` 的 tool 有 `_dry_run_<tool>` 函数
   - 失败仅日志告警，不阻断启动（业务方可能正在迭代）

4. **HITL 启动清扫**（spec §8.4）：
   ```python
   await hitl_manager.cleanup_pending_on_startup()
   # 服务重启 = 所有挂起 SSE 流已断，asyncio.Event 已丢
   # 残留 Redis pending 必须清扫避免 stale
   # 所有 ai_operation_log 行 status='pending_confirmation' 改为 'expired'
   ```

5. **AI router 注册条件**：
   ```python
   if settings.AI_MODULE_ENABLED:
       app.include_router(ai_chat_router, ...)
   ```

`false` 分支只注册 `/ai`/`/ai/**` disabled guard，统一返回 503 + `AI_MODULE_DISABLED`；fresh-process 启动测试同时校验 AI router、Service、Provider、Gateway、Registry 和 lifecycle 未加载。

---

## 5. LLM Provider 配置

### 5.0 平台控制面身份

Agent、Provider、Model 是 platform-global 配置。Agent 管理由默认租户的系统超级管理员使用普通登录会话维护；Provider、Model 和租户模型策略仍使用独立平台身份。需要维护这些资源时，完成 `alembic upgrade head` 后，用离线交互命令创建首个平台主体；密码只从终端
安全提示读取，不进入命令历史：

```bash
python -m tools.ops.platform_principal create \
  --principal-name platform_admin \
  --display-name "Platform Administrator" \
  --permission platform:ai:read \
  --permission platform:ai:write
```

权限必须显式逐项给出；只读审计主体只传 `platform:ai:read`。命令使用 PostgreSQL transaction
advisory lock 串行化，并在任意平台主体已经存在时拒绝，因此它只能初始化第一个主体，不能作为
日常 principal 管理旁路。密码必须为 12–72 bytes 且同时包含字母和数字；不接受密码 argv/env。

平台 token 默认 15 分钟，部署只允许通过 `PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES` 配置为
1–60 分钟，不继承 tenant access token 周期。数据库会在 principal password hash、status 或
permissions 变化时原子递增 `row_version`；disable 后重新 enable 也不会恢复旧 token，单独更新
`last_login_at` 不会误撤销会话。平台请求复验 principal 时持有 transaction `FOR SHARE` 到请求
结束，使进行中的请求与 password/status/permission 更新形成确定先后，避免复验后的撤权竞态。

`POST /platform/auth/login` 返回短期 `platform_access` token，不提供 refresh。调用专用
管理路径 `/platform/ai/providers/**`、Model 或 tenant model policy 时，
除 Bearer token 外必须同时发送
`X-Platform-Reason`、`X-Platform-Ticket`、`X-Correlation-ID`。平台操作先写授权审计再进入业务，
审计不可 UPDATE/DELETE；header 命中 password/token/API key/secret pattern 时返回
`PLATFORM_AUDIT_CONTEXT_SENSITIVE` 且不执行业务。审计只保存路由模板、query key 数量、
canonical source IP 和固定结果摘要；completion 使用 conflict-first 幂等并在未知持久化失败时
重试一次。不要把平台 token 放入 tenant Web 会话或交给普通租户管理员。

#### 5.0.1 Tenant registry、支持查询与 retention

新安装若同一平台主体需要 tenant 运维能力，应在首次 bootstrap 时显式追加所需权限。已有
已有平台主体的部署不得重跑 bootstrap，migration 也不会自动扩权；使用独立离线替换命令，并把
**完整目标权限集合**逐项列出（未列出的既有权限会被撤销）：

```bash
python -m tools.ops.platform_principal replace-permissions \
  --principal-name platform_admin \
  --permission platform:ai:read \
  --permission platform:ai:write \
  --permission platform:tenant:read \
  --permission platform:tenant:write \
  --permission platform:support:read \
  --permission platform:audit:retention \
  --reason "Enable reviewed tenant operations" \
  --ticket-id "OPS-5001" \
  --correlation-id "ops-5001-platform-permissions"
```

命令从安全终端提示读取当前平台密码，不接受密码 argv/env；先持久化 append-only 授权事件，
再在 advisory/row lock 内复验密码和 `row_version`。并发安全事实变化会拒绝本次替换，成功替换
由数据库递增安全版本并立即撤销旧 token。命令异常只输出稳定错误码，不打印密码或 traceback。

`/platform/tenants` 只提供 prepare/read/disable：prepare 必须带 16–128 位 `Idempotency-Key`，
只产生 `prepared/status=2` registry row，不创建管理员、角色、菜单或模型策略；没有 enable/delete，
Default Tenant 0 不能通过 API 禁用。这些租户运维操作要求平台 Bearer token 及三个审计
header。支持查询仅返回事件类别、结果、耗时和时间，不返回用户名、用户 ID、IP、User-Agent、
path 或请求参数。

retention 必须先调用 `/platform/tenants/{tenant_id}/audit-retention/preview`，再把相同 cutoff 和
两类 expected count 提交给 `.../purge`；任一计数变化都返回 stale 且零删除。它只删除目标 tenant
的 System operation/login audit，不处理 platform audit、AI Trace 或 unresolved 登录失败。
`PLATFORM_AUDIT_MIN_RETENTION_DAYS` 默认 90，可配置 30–3650 天；hosted login/activation
默认关闭。

#### 5.0.2 平台 AI CLI 与隔离报告

Agent 使用管理后台的「AI 助手 → Agent 管理」页面。Provider/Model 的平台运维先把短期 token 放入
`HOHU_PLATFORM_ACCESS_TOKEN`，再使用 `tools/ops/platform_ai.py` 的固定资源/动作命令；reason、
ticket、correlation 必填，包含 API key 的 Provider payload 只从权限受控的 JSON 文件读取，
不要放入命令行参数。CLI 禁止任意 path、redirect 和环境代理。独立平台 token 不适用于 Agent 接口，不再使用 CLI 的 Agent 子命令维护 Agent。

切换版本前运行 `tools/ops/audit_tenant_isolation.py --build-sha <当前提交> --output <报告路径>`。
脚本会先拒绝 dirty worktree，再复验参数与 checkout HEAD；一致后在 PostgreSQL
repeatable-read/read-only 快照中生成
确定性报告；任一 tenant NULL/orphan/cross-link、unique 冲突、未登记模型/namespace、hosted
containment 越界或 legacy/scoped 摘要差异都会返回非零。报告只包含计数和 digest。

### 5.1 通过平台运维 CLI

1. 获取短期 platform token，并准备权限受控的 Provider JSON 文件。
2. 用 `platform_ai.py providers create` 新增 Provider，再用 `models create` 新增模型。
3. 用 `providers test --provider-id ... --model-id ...` 测试已保存配置。
4. 用 `policies set --tenant-id ... --model-id ... --payload-file ...` 显式授权已 bootstrap tenant。

### 5.2 API Key 加密

`api_key` 用 Fernet 对称加密存储（密钥从 `SECRET_KEY` 派生）。任何响应均不返回原文、片段或
masked verifier，只返回 `credentialConfigured`；编辑时不提交该字段表示保持不变。历史 config
或 URL 中疑似凭据在平台投影前继续脱敏。

### 5.3 多 Provider 切换

聊天、Agent 管理和 Provider 管理使用独立模型契约，所有 adapter 均受统一 Provider egress 策略约束。

---

## 6. 监控 / 日志

### 6.1 关键日志事件

| 事件 | 级别 | 含义 |
|---|---|---|
| `prompt injection detected` | WARNING | §11.1 L2 pattern 命中 |
| `keyword_blocklist blocked chat` | WARNING | §11.2 用户输入命中项目自定义敏感词 |
| `user auto-disabled blocked chat` | WARNING | §11.4 用户被自动禁用尝试 chat |
| `super_admin injection threshold hit (NOT disabling)` | WARNING | §11.4 超管命中阈值但豁免 |
| `user auto-disabled for injection threshold` | WARNING | §11.4 用户首次被禁用 |
| `Redis unavailable during quota check` | ERROR | Redis down，写操作被拒（spec §2.6） |
| `AI Tool Registry 启动校验失败` | ERROR | tool 引用了不存在的 agent/perm |
| `tool not found` / `perm denied` | WARNING | 鉴权失败 |

### 6.2 审计表

| 表 | 用途 |
|---|---|
| `ai_operation_log` | 每次 tool 调用一行（status / risk / execution_mode / duration_ms / is_security_event / event_type） |
| `sys_login_log` | 登录日志（独立） |
| `sys_operation_log` | HTTP 审计（`/ai/*` 排除，避免双重审计） |

### 6.3 Prometheus 监控（✅ v1.5+ 已实现 2026-07-13，spec §6.3）

**8 个核心 metric**（详见 `app/modules/ai/metrics.py`）：

| Metric | 类型 | 标签 | 用途 |
|---|---|---|---|
| `ai_tool_calls_total` | Counter | tool, status, risk, execution_mode | tool 调用计数 |
| `ai_tool_call_duration_seconds` | Histogram | tool | P95 延迟 |
| `ai_hitl_pending_count` | Gauge | mode | HITL 挂起数 |
| `ai_hitl_wake_total` | Counter | mode, result | wake 成功/失败率 |
| `ai_hitl_pubsub_lost_total` | Counter | (无) | **多 worker pubsub 防丢失命中** |
| `ai_hitl_timeout_total` | Counter | mode | 5min TTL 超时 |
| `ai_quota_rejected_total` | Counter | level | L1/L2 配额拒绝 |
| `ai_security_events_total` | Counter | event_type | 注入/关键词/禁用/IP 拉黑 |

**/metrics endpoint**：`GET /metrics` 暴露 Prometheus exposition format。不进 OpenAPI 文档。

**网络隔离**：`/metrics` 不做鉴权（Prometheus 标准做法），生产用 nginx/ingress 限制只允许内网 / Prometheus scrape IP。

---

## 10. Prometheus + Alertmanager 接入

### 10.1 Prometheus scrape 配置

```yaml
# prometheus.yml
scrape_configs:
  - job_name: hohu-admin
    scrape_interval: 15s
    metrics_path: /metrics
    static_configs:
      - targets: ["hohu-admin.internal:8000"]
        labels:
          service: hohu-admin
          env: prod
```

### 10.2 Alertmanager 规则

预置规则见 `docs/monitoring/alerts.yml`：

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/rules/ai-tool-gateway.yml  # 复制自 docs/monitoring/alerts.yml
```

**核心告警**（详见 alerts.yml）：
- `AIPubSubMessageLossRateHigh`：redis_pubsub 模式消息丢失率 > 1%
- `AIHitlTimeoutRateHigh`：HITL 5min 超时率 > 10%
- `AIQuotaRejectionsHigh`：配额拒绝每秒 > 0.5
- `AISecurityEventsHigh`：安全事件每秒 > 0.5
- `AIToolFailureRateHigh`：tool 调用失败率 > 30%
- `AIRedisDownAffectingAI`：Redis 故障影响 AI 写操作

### 10.3 Grafana dashboard 建议

文档不预置 dashboard JSON（用户在自家 Grafana 自建更灵活）。常用 PromQL：

```promql
# AI tool 成功率（按 tool 分组）
sum(rate(ai_tool_calls_total{status="success"}[5m])) by (tool)
  / sum(rate(ai_tool_calls_total[5m])) by (tool)

# HITL 平均确认时长（5min P95）
histogram_quantile(0.95,
  sum(rate(ai_tool_call_duration_seconds_bucket{execution_mode="hitl"}[5m])) by (le, tool)
)

# 多 worker 模式健康度（pubsub 丢失率）
sum(rate(ai_hitl_pubsub_lost_total[5m]))
  / sum(rate(ai_hitl_wake_total{mode="redis_pubsub"}[5m]))

# 安全事件聚合视图（按 event_type 饼图）
sum(rate(ai_security_events_total[5m])) by (event_type)
```

### 10.4 资源占用

- Prometheus：单实例 < 100MB RAM（10 万样本/分钟够用）
- 应用侧开销：< 1% CPU（prometheus_client 是 in-process，无网络开销）

### 10.5 SSE 续传依赖（spec §3 v1.5+，2026-07-16 落地）

SSE 续传（HITL 期热接管）要求 **`AI_HITL_MODE=redis_pubsub`** + 多 worker 部署。

- 内网部署 / 单 worker：保持 `memory` 模式，续传端点返 410（前端提示“网络中断，请重新发起”）
- 移动端 / 不稳定网络：必须 `redis_pubsub` 模式，否则断流即取消（用户重新发对话，LLM 重跑成本可接受）

配置：
```env
AI_HITL_MODE=redis_pubsub       # 启用续传的硬约束
AI_SSE_RESUME_ENABLED=true      # 续传功能开关（默认开）
AI_HITL_OWNER_LOCK_TTL_SEC=60   # owner 锁 TTL（spec §2.3 SR-10 反例 5）
```

**修改 `AI_TOOL_TIMEOUT` 时务必同步检查 `AI_HITL_OWNER_LOCK_TTL_SEC`**：owner 锁 TTL 必须 ≥ `AI_TOOL_TIMEOUT`，否则 execute_tool 慢时锁先过期 → 新 worker B 抢锁双执行（spec §2.3 race 分析）。当前默认 `AI_TOOL_TIMEOUT=30s` + `AI_HITL_OWNER_LOCK_TTL_SEC=60s` 留 30s 余量。

详见 spec [`2026-07-13-sse-resume-design.md`](./specs/2026-07-13-sse-resume-design.md) / SR-9 / SR-10 / SR-11 / SR-12。


---

## 7. 升级 / 回滚

### 7.1 升级流程

以下流程适用于 AI 管理能力升级。每个目标环境都必须独立完成 migration、Provider egress 审计、scope-diff ACK、确定性 E2E 和真实 Provider 验证，不能复用其他环境的结果代替部署验收。

```bash
# 1. 拉新代码
git pull

# 2. 跑迁移（如 spec 加了新表 / 字段）
alembic upgrade head

# projection dependency 列的 legacy NULL 不可可靠回填，读取时按设计 fail closed

# 3. 幂等补齐新增菜单与权限码（存量升级）
hohu migrate

# 4. 只读审计存量 Provider/Model；非零表示需修改 URL/allowlist，脚本不改 enabled
uv run python -m tools.ops.audit_ai_provider_egress

# 5. 在旧服务仍运行时生成只读 scope-diff；退出码 2 表示存在待确认扩大项
BUILD_SHA=<new-build-sha> uv run python -m tools.ops.audit_data_scope_union \
  --output /protected/phase2-scope-preflight.json

# 6. 授权管理员复核报告后，把当前完整 report hash 写入受控部署变量
export DATA_SCOPE_UNION_ACK_SHA256=<reviewed-report-sha256>

# 7. 原子切换门禁：同一数据库 session lock 内停止 writer、重跑审计、
#    精确校验 ACK，并调用只接受同一 build SHA 的部署方切换/健康验证脚本
BUILD_SHA=<new-build-sha> uv run python -m tools.ops.audit_data_scope_union \
  --output /protected/phase2-scope-release.json \
  --verify-ack \
  --maintenance-command '["systemctl","stop","hohu-admin"]' \
  --switch-command '["/opt/hohu/bin/activate-and-verify","<new-build-sha>"]'
```

`--maintenance-command` 与 `--switch-command` 都是 JSON argv，执行时固定 `shell=False`。切换脚本必须原子激活与 `BUILD_SHA` 相同的不可变构建、启动服务，并完成传统 API 与 AI 查询健康验证后才返回 0。session advisory lock 从 maintenance 开始一直持有到该命令返回；ACK/hash 不一致时不会调用切换命令，服务保持维护态，必须按下节恢复旧构建。不要在正在提供服务的 active checkout 上原地覆盖文件。

### 7.2 回滚流程

```bash
# 1. 保持维护态并激活上一个不可变构建
/opt/hohu/bin/activate-and-verify <previous-build-sha>

# 2. 仅在本次升级确有 schema migration 且已完成专项验证时回滚 migration
alembic downgrade -1

# system:user:role-auth 是 add-only 兼容数据，
# 回滚 resolver 时保留，不删除角色关联。
```

若 `--verify-ack` 因报告漂移退出，先复核新的受限报告并生成新的受控 ACK；不得复用旧 hash。若 switch command 失败，脚本会释放数据库锁但不会自动恢复 writer，部署方必须在维护态激活并验证旧构建后再恢复流量。

### 7.3 紧急停用 AI

不需要回滚代码，单变量即可：

```bash
# .env
AI_MODULE_ENABLED=false
systemctl restart hohu-admin
```

业务模块完全不受影响；AI 路径稳定返回 503 + `AI_MODULE_DISABLED`。详见 `docs/AI-SECURITY.md` §1。

---

## 8. 故障处理

### 8.1 Redis 故障

**现象**：
- `ERROR ... Redis unavailable during quota check`
- high risk 写工具返回 `AI_REDIS_DOWN`
- low risk 工具的连续失败检查也拒绝

**根因**：spec §2.6 保守降级 — Redis 故障时所有写操作 + 安全检查拒绝，不静默放过。

**处理**：
1. 检查 Redis 进程 / 网络 / AUTH 配置
2. `redis-cli -h <host> -p <port> ping` 验证连通性
3. 恢复后服务**自动**恢复正常（无需重启）

### 8.2 DB 故障

**现象**：
- `/ai/chat` 500（get_current_user 查 DB 失败）
- tool 业务函数抛 `OperationalError`

**处理**：项目主流程也依赖 DB，DB down 是全站故障，按 DBA 流程恢复。

### 8.3 LLM Provider 故障

**现象**：
- `/ai/chat` SSE 流 emit `error` 事件（`errorText` 含 provider 错误）
- 前端 `$message.error("AI 错误: ...")`

**处理**：
1. 切换备用 Provider（管理后台 → 模型管理 → 启用备用 Provider）
2. 前端 chat 页下拉切换模型
3. 联系 Provider 厂商

### 8.4 Token 过期

**现象**：`/ai/chat` 返回 401 `TOKEN_EXPIRED`

**处理**：前端 axios 拦截器自动调 `/auth/refreshToken` 刷新，无需用户干预。SSE 流（不走 axios）需要用户重新登录。

### 8.5 LLM 失控循环（spec §11.6）

**现象**：LLM 反复调同一 tool 不收敛。

**根因**：`UsageLimits(request_limit=10, tool_calls_limit=5)` 兜底，超出后 PydanticAI 抛 `UsageLimitExceeded`，前端显示「AI 调用次数超限」。

**处理**：用户换种问法即可，无需运维介入。

### 8.6 HITL 5min TTL 超时

**现象**：用户没在 5 分钟内确认 → tool 返回 `AI_HITL_EXPIRED`。

**处理**：用户重新发起请求即可。

---

## 9. 性能调优（v2+ 待评估）

### 9.1 当前瓶颈

- **单 worker**：所有 SSE 流共享一个进程，HITL 挂起占用一个协程
- **DB session 池**：每个 tool 调用开独立 session（spec §6.3 事务隔离），高并发下池子紧张

### 9.2 v1.5+ 优化方向

- ✅ **切 `AI_HITL_MODE=redis_pubsub`，放开多 worker**（2026-07-13 落地，spec §8.4.1 / SR-7）
- 增加 DB 连接池大小（`DATABASE_POOL_SIZE`）
- LLM 响应流式 token 化（已实现），减少首字节延迟感知

### 9.3 监控指标（v2+）

- P95 tool 调用延迟（按 tool 名分桶）
- HITL 平均确认时间
- LLM token 消耗（按 user / agent 分桶）
- Redis 命中率（quota / failures / query_cache）

---

## 附录：相关文档

- spec：`docs/specs/2026-07-02-ai-tool-gateway-design.md`（§1-21 完整设计）
- 安全策略：`docs/AI-SECURITY.md`（紧急停用 / 漏洞报告 / 部署 checklist）
- 原型：`docs/prototype/12-ai-chat-tool-call.html` / `13-ai-hitl-drawer.html` / `14-ai-clarification.html`
- 静态检查：`tools/checks/check_ai_tools.py`（pre-commit + CI 双跑）
- seed 脚本：`scripts/seed_ai_agents.py`（7 个内置 Agent）
- prompt 升级：`app/modules/ai/seed_prompts.py`（空值/已知旧默认值安全升级）
- 初始化：`scripts/init_db.py`（菜单 + 权限码 + 管理员）
- 菜单增量同步：`scripts/sync_menus.py`（按 route_name / permission 去重，幂等补新增菜单与权限码）
- DataScope 切换检查：`tools/ops/audit_data_scope_union.py`（旧 API/旧 AI/新 resolver 报告、精确 ACK、跨切换 session 维护锁）

---

## Changelog

- **2026-09-23**：`bootstrap_platform_principal.py` 与 `replace_platform_principal_permissions.py` 合并为 `platform_principal.py`（`create` / `replace-permissions` 子命令）；移除已废弃的存量权限迁移脚本（`migrate_ai_mvp_permissions` / `migrate_phase2_authorization`），存量升级统一走 `sync_menus.py` 菜单增量同步。
- **2026-09-22**：明确 Agent 管理普通会话与平台维护身份的边界，补充压缩迁移的支持范围和升级指南。
- **2026-09-02**：增加独立平台身份 bootstrap、短期 token、AI read/write 权限和
  append-only 请求审计；tenant `R_SUPER` 不再代表平台权限。
- **2026-09-02**：补充安全字段自动撤销、独立 15–60 分钟 TTL、completion
  并发幂等、first-only bootstrap 竞态锁、路由模板审计和敏感上下文拒绝。
- **2026-09-02**：增加不可登录 tenant registry、脱敏支持查询、逐租户
  compare-and-delete retention，以及需当前密码和审计上下文的离线权限替换流程。
- **2026-08-24**：补充 AI Trace、HITL 删除与撤权复验、浏览器身份矩阵和真实 Provider 验证说明。
- **2026-08-17**：接入可信 tenant scope-diff、旧 API/AI 双 legacy 报告、role-auth 升级和跨代码切换 session 维护锁流程。
- **2026-08-15**：补充结果谱系迁移、query-cache v3、显式 Tool 权限和 Web 运行时撤权清理。
- **2026-08-15**：补充已保存 Provider test、统一 hardened egress、部署 allowlist/CIDR 配置及只读存量审计。
- **2026-08-14**：明确 AI 模块默认开启、关闭态统一 503，以及权限和历史结果的实时授权边界。
- **2026-07-13（二）**：加 Prometheus 监控接入（spec §6.3 v1.5+）。§6.3 改为已实现；新增 §10 Prometheus + Alertmanager 接入（scrape 配置 + alerts.yml 引用 + Grafana PromQL 建议）。
- **2026-07-13**：加 v1.5+ redis_pubsub 模式部署说明（spec §8.4.1 / SR-7 落地）。§1.2 拆分为模式 A（memory，单进程）/ 模式 B（redis_pubsub，可水平扩展）；§2 .env 示例更新；§4.1 启动检查说明 redis_pubsub 跳过 assertion；§9.2 标记 redis_pubsub 已完成。
- **2026-07-09**：初版。覆盖 spec §8.4 / §11.5 / §2.6 全部部署相关内容。9 节：环境 / 配置 / 迁移 / 启动检查 / LLM / 监控 / 升级 / 故障 / 性能。
