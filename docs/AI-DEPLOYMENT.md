# AI Tool Gateway 部署指南

本文件说明 **hohu-admin AI Tool Gateway**（spec `docs/specs/2026-07-02-ai-tool-gateway-design.md`）的生产部署流程。

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

**模式 A — `AI_HITL_MODE=memory`（默认，MVP）**：
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

| 资源 | MVP（< 100 用户） | 中等规模（1k 用户） |
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
# 整个 AI 模块全局开关。False = 不注册 6 个 AI router（紧急停用入口）
AI_MODULE_ENABLED=true

# ===== HITL 模式（spec §8.4 / §8.4.1）=====
# memory（默认，MVP）：进程内 asyncio.Event；强制 WEB_CONCURRENCY=1，禁止多 pod
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

# ===== LLM 调用参数 =====
AI_MAX_TOKENS=4096
AI_TEMPERATURE=0.7

# ===== Redis / DB（继承项目主配置）=====
REDIS_URL=redis://127.0.0.1:6379/0
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
SECRET_KEY=<strong-random>      # JWT 签名，必须强随机
```

### 生产部署 checklist（spec §11.5 §6）

- [ ] `AI_MODULE_ENABLED` 按需设置（建议生产首次部署设 `false`，运维熟悉后再开）
- [ ] HITL 模式选择：
  - [ ] `AI_HITL_MODE=memory`：`WEB_CONCURRENCY=1`（强制）
  - [ ] `AI_HITL_MODE=redis_pubsub`：可放开 `WEB_CONCURRENCY`；验证 Redis 连接池大小 ≥ `max_concurrent_hitl_streams + 10`
- [ ] `SECRET_KEY` 强随机值（**不 reuse dev 默认**）
- [ ] 数据库账号最小权限（不用 superuser）
- [ ] Redis 启用 AUTH + 网络隔离
- [ ] LLM API Key 加密存储（Fernet，已实现）
- [ ] HTTPS 全链路（Nginx / Caddy TLS 终止）
- [ ] 速率限制中间件启用（`RATE_LIMIT_API`）
- [ ] 审计日志保留 ≥ 90 天（`ai_operation_log` / `sys_login_log` / `sys_operation_log`）
- [ ] ERROR 日志告警接入

---

## 3. 数据库迁移 + seed

### 3.1 创建迁移

```bash
# 新增 AI 相关字段（trace_id / agent_code / is_security_event 等）
alembic upgrade head
```

关键迁移：`c7d8e9f0a1b2_add_ai_tool_gateway_tables.py`（3 张新表 + ALTER 现有表 + 6 个索引）。

### 3.2 seed 内置 Agent + 权限码

```bash
# 7 个内置 Agent（user_mgmt / role_mgmt / dept_mgmt / config_mgmt / provider_mgmt / job_mgmt / shared）
uv run python scripts/seed_ai_agents.py

# 同步菜单 + 5 个权限码（ai:agent:list/add/edit/delete + ai:trace:view）
uv run python scripts/init_db.py
```

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

5. **AI router 注册条件**（spec §11.5）：
   ```python
   if settings.AI_MODULE_ENABLED:
       app.include_router(ai_chat_router, ...)  # 6 个 AI router
   ```

---

## 5. LLM Provider 配置

### 5.1 通过管理后台 UI

1. 登录管理后台 → **AI 助手 → 模型管理**
2. 新增 Provider（`provider_code` / `name` / `api_key` / `base_url`）
3. 启用至少一个模型（如 `doubao-seed-2-1-pro-260628` / `gpt-4o`）
4. 测试模型（POST `/ai/provider/test-model`）

### 5.2 API Key 加密

`api_key` 用 Fernet 对称加密存储（密钥从 `SECRET_KEY` 派生）。列表 / 编辑返回 masked 值（`sk-***xxx`），编辑时留空表示不变。

### 5.3 多 Provider 切换

前端 chat 页右上角下拉选择模型，后端按 `modelId` 路由到对应 Provider。

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

- 内网部署 / 单 worker：保持 `memory` 模式，续传端点返 410（前端退化为 MVP 行为，提示"网络中断，请重新发起"）
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

```bash
# 1. 拉新代码
git pull

# 2. 跑迁移（如 spec 加了新表 / 字段）
alembic upgrade head

# 3. 重启服务（lifespan 会自动清扫 pending confirmation）
systemctl restart hohu-admin
# 或 docker compose restart hohu-admin
```

### 7.2 回滚流程

```bash
# 1. 回滚代码
git checkout <previous-tag>

# 2. 回滚迁移（谨慎，可能丢数据）
alembic downgrade -1

# 3. 重启
systemctl restart hohu-admin
```

### 7.3 紧急停用 AI

不需要回滚代码，单变量即可：

```bash
# .env
AI_MODULE_ENABLED=false
systemctl restart hohu-admin
```

业务模块完全不受影响。详见 `docs/SECURITY.md` §1。

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
- 安全策略：`docs/SECURITY.md`（紧急停用 / 漏洞报告 / 部署 checklist）
- 原型：`docs/prototype/12-ai-chat-tool-call.html` / `13-ai-hitl-drawer.html` / `14-ai-clarification.html`
- 静态检查：`scripts/check_ai_tools.py`（pre-commit + CI 双跑）
- seed 脚本：`scripts/seed_ai_agents.py`（7 个内置 Agent）
- 初始化：`scripts/init_db.py`（菜单 + 权限码 + 管理员）

---

## Changelog

- **2026-07-13（二）**：加 Prometheus 监控接入（spec §6.3 v1.5+）。§6.3 改为已实现；新增 §10 Prometheus + Alertmanager 接入（scrape 配置 + alerts.yml 引用 + Grafana PromQL 建议）。
- **2026-07-13**：加 v1.5+ redis_pubsub 模式部署说明（spec §8.4.1 / SR-7 落地）。§1.2 拆分为模式 A（memory，MVP）/ 模式 B（redis_pubsub，可水平扩展）；§2 .env 示例更新；§4.1 启动检查说明 redis_pubsub 跳过 assertion；§9.2 标记 redis_pubsub 已完成。
- **2026-07-09**：初版。覆盖 spec §8.4 / §11.5 / §2.6 全部部署相关内容。9 节：环境 / 配置 / 迁移 / 启动检查 / LLM / 监控 / 升级 / 故障 / 性能。
