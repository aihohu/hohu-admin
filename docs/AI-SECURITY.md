# Security Policy

本文件说明 **hohu-admin** 的安全设计、配置开关和漏洞报告流程。**所有部署前必读**。

### 2026-09-18：Agent 管理身份更新

Agent 管理使用现有用户登录会话；默认系统中的启用 `R_SUPER` 为系统超级管理员角色，其他租户中的该角色称租户管理员，仅在所属租户内生效。用户名 `admin` 不再提供权限旁路；初始化 admin 因绑定系统角色而有权限，改名不影响角色权限，撤回/停用角色则失权。

`/platform/ai/agents` 的列表、详情、模型选项和更新只接受系统角色授权，独立平台 token 不再进入这些接口。其他平台维护 API 保持独立鉴权。全局 Agent 操作通过 `sys_operation_log` 的 `audit_scope=platform` 记录真实用户身份、原因、工单和关联 ID；授权意图独立持久化，成功记录与配置变更同事务，文本变更记录指纹，避免提示词或密钥落入审计正文。

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

目标契约固定为：

- 默认 `AI_MODULE_ENABLED=true`，开发与生产语义一致；关闭只用于紧急熔断或明确的无 AI 部署，不作为日常授权手段。
- `false` 时不得初始化 AI 业务 router、Service、Provider、Gateway 或 Registry；现有 HTTP 审计、用户管理、角色、字典等非 AI 模块不受影响。
- 全部 `/ai/**` 对标准 HTTP 方法（含 `TRACE`、`CONNECT`）统一返回 HTTP 503、`errorCode=AI_MODULE_DISABLED`；不能依赖 router 未注册产生的 404/405，也不能让前端自行猜测模块状态。
- fresh-process 测试必须证明关闭态统一返回 503，且 AI router、Service、Provider、Gateway、Registry 和 lifecycle 不会加载或初始化。

### 紧急下线 checklist

1. `.env` 设 `AI_MODULE_ENABLED=false`
2. 重启服务（`uvicorn` 或 `gunicorn` 重启）
3. 验证任意 `/ai/**` 返回 503 + `AI_MODULE_DISABLED`
4. 检查启动日志，确认 Provider/Gateway/Registry 未初始化且非 AI API 正常
5. 通报团队 + 提 issue 复盘

---

## 2. AI 安全特性清单（已实现）

模块默认开启不等于登录用户默认获得 AI 能力。安全边界由入口权限、tenant/owner、显式 Role-Agent、Tool 精确归属与功能权限、多角色 DataScope、HITL 锁后复验共同组成；`shared` 与 R_SUPER 不构成 Agent/Tool 权限旁路，历史持久化结果及其跨轮依赖也必须按当前授权重新投影。

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
| Provider egress | 精确 origin + 全部 DNS/IP + 连接固定 + 禁 redirect/环境代理 + timeout/size/concurrency/retry | `app/modules/ai/core/provider_egress.py` | ✅ |
| super_admin gate | `super_admin_only=True` 的 tool 仅超管调用 | `AiToolMeta` 声明 | ✅ |
| 静态检查 | `scripts/check_ai_tools.py` pre-commit + CI | `.pre-commit-config.yaml` | ✅ |

### 未实现 / 留 v2+

| 项 | 说明 | 临时方案 |
|---|---|---|
| L2 keyword_blocklist | spec §11.2，依赖 `system_config.ai:guardrail:keyword_blocklist` 表 | 暂用 L2 injection_detector + 手动 ad-hoc 过滤 |
| L3 通用 sanitize | spec §11.1 L3 层，每 tool args 形态不同难通用 | 当前由 L3 数据鉴权 + L4 字段白名单兜底 |
| IP 级自动拉黑 | spec §11.4 单 IP `mass_permission_denied` ≥ 50 拉黑 | 仅用户级自动禁用生效 |
| Prometheus 告警 | `ai_super_admin_injection_alert` 等指标 | 仅日志告警（`logger.warning`） |

---

## 3. 如何启用 AI 内置 Agent

AI 模块默认开启，但 Agent 能力必须通过显式权限和角色绑定启用，不能依赖 shared 或超级管理员 Agent/Tool 旁路。未启用的业务 Agent 保持禁用。

### 步骤

1. **数据库迁移**：`alembic upgrade head`（创建 `ai_agent` / `role_ai_agent` / `ai_operation_log` 表）
2. **seed 内置 Agent、prompt、菜单与权限码**：
   ```bash
   uv run python scripts/seed_ai_agents.py
   uv run python scripts/seed_agent_prompts.py  # 安全升级内置默认 prompt，保留自定义值
   uv run python scripts/init_db.py  # 含菜单 + 权限码同步
   ```
   存量升级另执行 `uv run python scripts/migrate_ai_mvp_permissions.py`，幂等补入口权限与 R_SUPER 绑定，并保留已有 Agent、Role-Agent 和工具启用状态；不会给 shared-only 普通角色扩权。
3. **配置 LLM Provider**（管理后台 → 模型管理）：保存并启用至少一个模型。
4. **显式授权**：按唯一基线配置 AI 入口权限、Role-Agent 绑定和 Tool 权限。
5. **重启服务**：`AI_MODULE_ENABLED=true`（默认）。

### 验证

```bash
curl -X POST http://127.0.0.1:8000/ai/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"trigger":"submit-message","id":"test","messages":[{"id":"m1","role":"user","parts":[{"type":"text","text":"你好"}]}]}'
```

除正向 SSE 外，还必须验证无入口、无绑定、无 Tool 权限和数据越界账号均被后端拒绝。

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
- 漏洞 bounty / 奖励：项目目前不提供现金奖励，可在报告者同意后公开致谢

### 已知漏洞历史

参见 [`docs/security-advisories/`](./security-advisories/)（如有）。

---

## 5. 部署安全 checklist

生产部署前**逐项确认**：

- [ ] `AI_MODULE_ENABLED=true`；若因紧急熔断设为 false，已验证 `/ai/**` 统一 503 且 AI 业务组件未初始化
- [ ] `ai:chat:use`、active Role-Agent、Tool 权限和多角色 DataScope 四层拒绝测试通过；soft-disabled Role-Agent 不贡献 grantable authority
- [ ] DataScope scope-diff 使用服务端可信 tenant，并在同一 session 维护锁内完成停止 writer、重审计、精确 ACK、同 build 切换和验证
- [ ] Provider/Model 的保存、测试和运行调用均通过统一出站安全边界
- [ ] Agent 管理复用系统超级管理员的普通登录会话；其他租户管理员及独立平台 token 均被拒绝。Provider/Model 管理仍要求独立 `platform_access` token；管理操作提供 reason/ticket/correlation
- [ ] 全局 AI 管理只使用 `/platform/ai/**` 和固定路由 CLI；旧 `/ai/admin/agents/**`、
      `/ai/provider/**` 不可达，Provider 响应没有密钥原文、片段或可复用 verifier
- [ ] tenant model policy 目标只来自 route-bound PlatformContext；默认模型切换不影响其他 tenant，
      被任一 tenant 引用的 Provider/Model 必须先撤销策略才能删除
- [ ] `PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES` 在 1–60 分钟内；principal 安全字段变更触发
      `row_version` 自动递增，disable→enable 后旧 token 仍失败；请求复验的 `FOR SHARE` 已把
      在途操作与撤权 UPDATE 线性化
- [ ] `sys_platform_audit_log` 的授权事实先于业务副作用持久化，数据库 UPDATE/DELETE 触发器已启用
- [ ] completion 并发重放汇合为一行、状态冲突拒绝；审计只含路由模板和 allowlisted summary，
      secret header 被拒绝且错误日志不打印 traceback/SQL 参数
- [ ] bootstrap 显式列出最小权限，只允许创建首个主体，密码不进入 argv/env，双进程竞态仅一方成功
- [ ] 既有主体新增 tenant 运维权限时使用离线 permission-replace；提供当前密码和完整目标权限集，
      不重跑 bootstrap、不由 migration 自动扩权，变更后旧 token 已失效
- [ ] tenant prepare 始终为 `prepared/status=2` 且不产生用户/角色/菜单/model policy；
      disable 原子更新 lifecycle/status 并递增安全版本，Default Tenant 不可禁用
- [ ] 平台支持查询仅返回语义事件投影；username/user ID/IP/User-Agent/path/request params 不可见
- [ ] System audit retention 已先 preview 再按 expected counts compare-and-delete；仅删除目标 tenant
      的 tenant-scope operation/login log，platform audit、AI Trace、unresolved 均未受影响
- [ ] `WEB_CONCURRENCY=1`（强制，spec §8.4 单 worker）
- [ ] `SECRET_KEY` 使用强随机值（不 reuse dev 默认）
- [ ] `JWT_SECRET` 与 `SECRET_KEY` 不同
- [ ] 数据库账号最小权限（不用 superuser）
- [ ] Redis 启用 AUTH + 网络隔离
- [ ] LLM Provider API Key 加密存储（Fernet，已实现）
- [ ] tenant isolation 报告绑定 checkout HEAD、source/schema digest，并在 read-only 快照中得到
      `riskCount=0`；报告不含业务行、PII、token/password/API key
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
| `app/middleware/platform_audit_middleware.py` | 平台操作 completed 事件与授权事件关联 |
| `app/modules/platform/` | 独立平台 principal、token、permission 和 append-only 审计 |
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

- **2026-09-22**：Agent 管理改为默认租户系统角色授权，复用普通登录会话并记录真实用户审计；Provider/Model 与租户运维保留独立平台鉴权。
- **2026-09-03**：将全局 AI 管理迁入专用平台 API/CLI，增加 tenant model policy
  管理、write-only credential 投影、tenant namespace 与确定性只读隔离报告门禁。
- **2026-09-02**：建立独立平台身份与请求审计边界；tenant `R_SUPER` 不再能修改
  platform-global Agent/Provider/Model。
- **2026-09-02**：建立不可登录 tenant registry、最小化支持查询和受保护 retention；
  既有平台主体通过当前密码与 append-only 审计保护的离线流程显式替换权限。
- **2026-08-24**：补充 AI Trace、HITL 删除与撤权复验、浏览器身份矩阵和真实 Provider 验证要求。
- **2026-08-17**：锁定 active Role-Agent 委派上界、可信 tenant 双 legacy scope-diff，以及跨代码切换 session 维护锁。
- **2026-08-15**：补充显式 R_SUPER Tool 权限、跨轮结果依赖和 Web 运行时撤权清理。
- **2026-08-15**：补充 Provider 全路径 hardened egress、运行时 quarantine 和只读存量审计。
- **2026-08-14**：明确 AI 模块默认开启、关闭态统一 503、无 shared/R_SUPER Agent 旁路、历史结果实时授权和统一 Provider egress。
- **2026-07-08**：初版。覆盖 spec §11.5 全部 4 节（模块开关 / 安全清单 / Agent 启用 / 漏洞报告）。
