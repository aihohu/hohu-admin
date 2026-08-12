# Security Policy

本文件说明 **hohu-admin** 的安全设计、配置开关、漏洞报告流程。**所有部署前必读**。

## 适用范围

- 后端 `hohu-admin`（FastAPI + PostgreSQL + Redis）
- 前端 `hohu-admin-web`（Vue 3 + NaiveUI）
- AI Tool Gateway（spec `docs/specs/2026-07-02-ai-tool-gateway-design.md` §11）

---

## 1. AI 模块全局开关（紧急停用入口）

生产环境出现 AI 异常（注入攻击成功 / 越权 / 数据泄漏）时，**单变量即可下线整个 AI 模块**，不影响其他业务：

```bash
# .env
AI_MODULE_ENABLED=false
```

`AI_MODULE_ENABLED=false` 时：
- `app/main.py` **不注册** 6 个 AI router（`/ai/chat` / `/ai/confirm` / `/ai/conversation` / `/ai/provider` / `/ai/operation-log` / `/ai/query-cache`）
- 现有 HTTP 审计、用户管理、角色、字典等业务模块**完全不受影响**
- 前端 AI 入口会因 404 自然失效，用户看到「AI 暂不可用」

默认 `AI_MODULE_ENABLED=true`（开发环境），生产部署时按需切换。

### 紧急下线 checklist

1. `.env` 设 `AI_MODULE_ENABLED=false`
2. 重启服务（`uvicorn` 或 `gunicorn` 重启）
3. 验证 `/docs` 不再含 AI tag
4. 通报团队 + 提 issue 复盘

---

## 2. AI 安全特性清单（已实现）

按 spec §11 防御层级：

| 层 | 机制 | 配置 | 状态 |
|---|---|---|---|
| L1 工具可见性 | session 级按 perms 过滤（`compute_available_tools`） | RBAC + Role → Menu | ✅ |
| L2 输入 pattern | `injection_detector` 7 类攻击模式 → 强制 HITL | `app/modules/ai/agents/safety/injection_detector.py` | ✅ |
| L2 自动禁用 | 单用户 1h ≥ 5 次注入命中 → 禁用 24h | 阈值硬编码（INJECTION_THRESHOLD_PER_HOUR=5） | ✅ |
| L2 超管豁免 | 超管注入命中只告警，不禁用 | 防锁死运维入口 | ✅ |
| L3 数据鉴权 | `ensure_targets_in_scope` list 版 + data_scope filter | RBAC role.data_scope | ✅ |
| L3 字段白名单 | `allowed_filters` / `allowed_group_by` 防高基数字段 | `AiToolMeta` 声明 | ✅ |
| L4 敏感数据 | `sensitive_input` 不进函数签名 + `sensitive_output` 全局黑名单 | `app/modules/ai/agents/gateway/sensitive.py` | ✅ |
| L4 历史脱敏 | `redact_secrets` 4 类正则 + MIME 白名单 | `app/modules/ai/agents/gateway/redact.py` | ✅ |
| HITL 强制 | destructive / hitl_always / 注入命中 → 强制人工确认 | `classify_execution_mode` | ✅ |
| Agent loop 上限 | LLM 单次会话最多 10 次请求 / 5 次 tool 调用 | `UsageLimits` | ✅ |
| super_admin gate | `super_admin_only=True` 的 tool 仅超管调用 | `AiToolMeta` 声明 | ✅ |
| 静态检查 | `scripts/check_ai_tools.py` pre-commit + CI | `.pre-commit-config.yaml` | ✅ |

### 未实现 / 留 v2+

| 项 | 说明 | 临时方案 |
|---|---|---|
| L2 keyword_blocklist | spec §11.2，依赖 `system_config.ai:guardrail:keyword_blocklist` 表 | 暂用 L2 injection_detector + 手动 ad-hoc 过滤 |
| L3 通用 sanitize | spec §11.1 L3 层，每 tool args 形态不同难通用 | MVP 由 L3 数据鉴权 + L4 字段白名单兜底 |
| IP 级自动拉黑 | spec §11.4 单 IP `mass_permission_denied` ≥ 50 拉黑 | 仅用户级自动禁用生效 |
| Prometheus 告警 | `ai_super_admin_injection_alert` 等指标 | 仅日志告警（`logger.warning`） |

---

## 3. 如何启用 AI 内置 Agent

MVP 阶段 AI 默认**关闭**，需手动启用：

### 步骤

1. **数据库迁移**：`alembic upgrade head`（创建 `ai_agent` / `role_ai_agent` / `ai_operation_log` 表）
2. **seed 7 个内置 Agent + 5 个权限码**：
   ```bash
   uv run python scripts/seed_ai_agents.py
   uv run python scripts/seed_agent_prompts.py  # 安全升级内置默认 prompt，保留自定义值
   uv run python scripts/init_db.py  # 含菜单 + 权限码同步
   ```
3. **配置 LLM Provider**（管理后台 → 模型管理）：填 `apiKey` + `baseUrl`，启用至少一个模型
4. **绑定 Role → Agent**（`role_ai_agent` 表）：哪个角色能用哪个 Agent
5. **重启服务**：`AI_MODULE_ENABLED=true`（默认）

### 验证

```bash
curl -X POST http://127.0.0.1:8000/ai/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"trigger":"submit-message","id":"test","messages":[{"id":"m1","role":"user","parts":[{"type":"text","text":"你好"}]}]}'
```

返回 SSE 流 + AI 文本回复即启用成功。

### 关闭

`.env` 设 `AI_MODULE_ENABLED=false` 重启即可（见 §1）。

---

## 4. 漏洞报告流程

### 报告渠道

**不要在公开 GitHub issue 提交安全漏洞**。请通过以下任一渠道私密报告：

- 邮件：**security@hohu.example**（Replace with your security email）
- GitHub Security Advisory：仓库 → Security → Report a vulnerability（Private fork）

### 报告内容（请尽量提供）

- 受影响版本（git commit hash 或 release tag）
- 复现步骤（最小化 PoC）
- 影响评估（数据泄漏 / 越权 / RCE 等）
- 建议修复方向（可选）

### SLA / 响应时间

| 阶段 | 时间窗口 | 行动 |
|---|---|---|
| 接收确认 | 24h 内 | 维护者确认收到 + 初步评估 |
| 初步评估 | 72h 内 | 严重程度分级 + 修复方案讨论 |
| 补丁发布 | 7 天内（critical）/ 30 天内（high）/ 90 天内（medium-low） | 发布修复版本 + CVE 申请（如需） |
| 公开披露 | 补丁发布后 14 天 | 公开 advisory（与报告者协商） |

### 责任披露原则

- 报告者**不应**在补丁发布前公开漏洞细节
- 维护者**承诺**致谢报告者（如愿意）+ 不追溯性追责善意研究者
- 漏洞 bounty / 奖励：MVP 阶段无现金奖励，仅公开致谢

### 已知漏洞历史

参见 [`docs/security-advisories/`](./security-advisories/)（如有）。MVP 阶段无已知未修复漏洞。

---

## 5. 部署安全 checklist

生产部署前**逐项确认**：

- [ ] `AI_MODULE_ENABLED` 按需设置（生产可设 false 直到运维熟悉）
- [ ] `WEB_CONCURRENCY=1`（强制，spec §8.4 单 worker）
- [ ] `SECRET_KEY` 使用强随机值（不 reuse dev 默认）
- [ ] `JWT_SECRET` 与 `SECRET_KEY` 不同
- [ ] 数据库账号最小权限（不用 superuser）
- [ ] Redis 启用 AUTH + 网络隔离
- [ ] LLM Provider API Key 加密存储（Fernet，已实现）
- [ ] HTTPS 全链路（Nginx / Caddy TLS 终止）
- [ ] 速率限制中间件启用（`RATE_LIMIT_API`）
- [ ] 审计日志保留 ≥ 90 天（`ai_operation_log` / `sys_login_log` / `sys_operation_log`）
- [ ] 监控告警接入（至少 ERROR 日志告警）

---

## 6. 安全相关代码索引

| 文件 | 职责 |
|---|---|
| `app/core/auth.py` | `require_permissions` + `super_admin_only` 装饰器 |
| `app/core/rbac.py` | `is_super_admin` 判定 |
| `app/core/security.py` | JWT create/verify + bcrypt |
| `app/core/exceptions.py` | 领域异常层级（含 `error_code` 给前端 i18n） |
| `app/middleware/audit_middleware.py` | HTTP 审计中间件（`/ai/*` 排除，走 AI 独立审计） |
| `app/middleware/rate_limit_middleware.py` | IP 级速率限制 |
| `app/modules/ai/agents/safety/injection_detector.py` | L2 注入检测 |
| `app/modules/ai/agents/safety/auto_disable.py` | §11.4 用户级自动禁用 |
| `app/modules/ai/agents/safety_preamble.py` | SAFETY_PREAMBLE 6 条规则 + dynamic_block |
| `app/modules/ai/agents/gateway/executor.py` | Gateway 统一执行入口（perm + capacity + HITL + 脱敏） |
| `app/modules/ai/agents/gateway/sensitive.py` | L4 输出脱敏 |
| `app/modules/ai/agents/gateway/redact.py` | L4 历史脱敏 |
| `app/modules/ai/agents/gateway/targets.py` | L3 数据鉴权 helper |
| `scripts/check_ai_tools.py` | tool 接入合规静态检查 |

---

## Changelog

- **2026-07-08**：初版。覆盖 spec §11.5 全部 4 节（模块开关 / 安全清单 / Agent 启用 / 漏洞报告）。
