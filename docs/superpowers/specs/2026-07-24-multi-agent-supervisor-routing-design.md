# Multi-Agent Supervisor 路由设计

**Status**: ✅ Plan 已完成（2026-07-26）— 后端 PR [aihohu/hohu-admin#7](https://github.com/aihohu/hohu-admin/pull/7) + 前端 PR [aihohu/hohu-admin-web#3](https://github.com/aihohu/hohu-admin-web/pull/3) squash merge，732/732 测试绿，E2E 3/3 通过。实施过程决策记录见 plan `2026-07-25-multi-agent-supervisor-routing.md` 头部"✅ Plan 已完成"块。  
**Created**: 2026-07-24  
**Revised**: 2026-07-24（v2: factual errors + design gaps；v3: 内部矛盾 + clarification 协议 + 审计完备性；**v4: LLM-only router + 无状态 clarification + routing_feedback + legacy null 开关**，详见 §16 修订日志）  
**Owner**: hohu core team  
**Depends on**: spec `2026-07-02-ai-tool-gateway-design.md` §5.4 / §14 / §17.2；`hohu-admin` 已存在 `ai_agent` 表、`GET /ai/agents` 端点（`app/modules/ai/api/agent.py:25`）、`@ai_tool(agent=...)` 分组。

----

## 1. 背景与目标

`hohu-admin` 当前已内置 **7 个** Agent（`shared` / `user_mgmt` / `role_mgmt` / `config_mgmt` / `dept_mgmt` / `provider_mgmt` / `job_mgmt`，见 `scripts/seed_ai_agents.py:29`），前端已支持手动选择 Agent。但用户需要**先选对 Agent 再问问题**，体验割裂。

本设计目标：
- 用户发送消息时，Supervisor 自动选择最合适的 Agent。
- 保留手动选择能力作为覆盖（override）。
- 模糊时通过 clarification 卡片让用户确认，而不是瞎猜。
- 不破坏现有单 Agent 执行路径、安全、审计和配额体系。
- **提供路由反馈闭环**：用户可标注"选错 Agent"，反馈数据用于 description / 关键词调优（v4 新增）。

----

## 2. 非目标

本期**不做**：
- 一轮对话内多 Agent 协作（Agent A 调完再调 Agent B）。
- Agent marketplace / 外部插件动态注册 Agent。
- 自定义 Supervisor 模型（先用全局默认，后续通过 `sys_config.ai:supervisor_model` 扩展）。
- 基于多轮历史的上下文感知路由（只看当前消息）。

**会话内 Agent 切换策略**单独说明（见 §5.3）：既不允许"中途任意切"，也不强制"首轮定终身"——粘滞 + 显式 `"auto"` 双开关。

----

## 3. 架构

新增 `app/modules/ai/agents/supervisor/` 模块，职责单一：**根据用户消息选 Agent**。

```
用户输入 POST /ai/chat
  │
  ▼
[既有安全前置检查]（keyword_blocklist / forbidden_topics / forbidden_urls / injection）
  │ 命中 → 现有短路逻辑（AI_KEYWORD_BLOCKED 等），不进入路由
  ▼
  ├── agentCode 显式指定（如 user_mgmt） ──► 跳过路由，直接用该 Agent（现有行为）
  ├── agentCode = null（未传） ─────────► 粘滞：用本会话上轮 agent_code（无则 fallback "auto"）
  └── agentCode = "auto" ────────────────► AgentRouter.route(deps, message)
                                              │
                                              ├── LLMClassifier（候选 = 用户可见 Agent）
                                              │     候选 description 动态拼进 prompt → JSON {agent_code}
                                              │     JSON 鲁棒解析（json.loads → 正则截 {...} → 失败降级）
                                              ├── 解析成功 + code 在候选 ─► agent_code
                                              ├── 无 Provider / LLM 失败 / 仍模糊 ─► clarification_required（无状态 SSE event）
                                              └── 候选集空 ─► AI_ROUTING_FAILED
  │
  ▼
chat_service.build_chat_deps(agent_code=...) → create_agent → 原有单 Agent 执行流（HITL / quota / audit 不变）
                                              │
                                              ▼
                                          ai_routing_log 记录决策（trace_id 关联 ai_operation_log）

[前端反馈循环]
  assistant 消息渲染后 → 用户点"选错 Agent？" → POST /ai/messages/{id}/routing-feedback
                                              │
                                              ▼
                                          ai_message.routing_feedback = 'wrong'（用于调优 description）
```

核心原则：
- **Supervisor 只替换"选 Agent"这一步**，不侵入 tool executor、HITL、SSE 流式逻辑。
- **路由前复用现有安全检查**（keyword / topic / injection），不绕过；同时修复"敏感词只能在路由后才拦截"的隐患。
- **路由决策全程可审计**（§7.2 新增 `ai_routing_log` 表）。
- **v4 简化**：砍掉规则阶段（strong/weak keyword），LLM 直接路由；clarification 协议无状态化（无 Redis confirmationId）；加路由反馈闭环。详见 §16。

----

## 4. 数据流

### 4.1 主路径

1. **前端发消息**  
   `POST /ai/chat` body 中 `agentCode` 三种语义：
   - 具体 code（如 `user_mgmt`）→ 跳过路由。
   - 字面量 `"auto"` → 强制 Supervisor 路由。
   - 不传 / `null` → **会话粘滞**：若 `ai_conversation.agent_code` 已有值则复用，否则等价 `"auto"`。

   > 这是相对当前实现的**行为变更**：现状下 `null` 默认 fallback 到 `DEFAULT_AGENT_CODE = "user_mgmt"`（`chat_service.py:26,129`）。本期改为会话粘滞 + Supervisor。客户端如需旧行为，请显式传 `"user_mgmt"`。详见 §15 兼容性。

2. **既有安全前置检查（顺序重排：从路由后提前到路由前）**  
   保留 `chat.py` 现有的 `keyword_blocklist` / `forbidden_topics` / `forbidden_urls` / `injection_detector`。命中即短路返回 `AiErrorEvent`，**根本不进入 Supervisor**。这同时修复了现有架构里"敏感词要在路由后才拦截"的隐患（攻击者可借 Supervisor LLM 间接泄露敏感词存在性）。

3. **Supervisor 路由**  
   - `AgentRouter` 从 `ai_agent` 表加载当前用户**有权限且已启用**的 Agent。
   - LLM 阶段：把候选 Agent 的 `name` / `description` 拼进 prompt，返回 `agent_code` JSON（无规则阶段，详见 §5.1）。
   - JSON 解析失败 / `agent_code` 不在候选 / 无可用 Provider / 候选集空 → `clarification_required`（无状态 SSE event，详见 §6.2）。

   > **延迟说明**：路由发生在 SSE 流开始之前。LLM 阶段取决于模型（通常 200–800ms，用 haiku / 4o-mini / qwen-flash 等小模型可压到 100–300ms）。首期接受该延迟；后续可在 stream 开头 emit `routing` 事件提升感知。

4. **用户消息持久化时机调整（关键修复，同时修现有 bug）**  
   现状 `chat.py:226-239` 在路由前就 `save_user_message + commit`，**且 keyword/topic/injection 检查（`chat.py:308-415`）在 save 之后**——命中敏感词时已产生孤儿 user 消息。本期改为：
   - 安全检查 → 路由 → 都通过后才 `save_user_message`。
   - clarification 时 → **不持久化** user 消息；前端把原文暂存在本地，等用户选完 Agent 后连同 `agentCode` 一起重发。
   - 路由 LLM 异常 / 候选集空 → emit `AI_ROUTING_FAILED`，不持久化 user 消息，前端提示重试。
   - 详见 §13 决策 13（同时修现有 orphan bug）。

5. **执行**  
   用选中的 `agent_code` 走 `build_chat_deps` → `create_agent` → stream。最终 `agent_code` 写入 `ai_conversation.agent_code` 和每条 `ai_message.agent_code`（§7.1b 新增列，让历史会话也能按消息粒度还原 Agent）。

6. **审计（覆盖所有 `/ai/chat` 请求）**  
   所有 `/ai/chat` 请求都写一条 `ai_routing_log`（§7.2），不仅限 `"auto"`：`reason` 区分 `llm_resolved` / `clarification` / `session_sticky` / `manual_override` / `supervisor_disabled` / `safety_blocked` / `quota_exceeded` / `no_provider` / `no_candidates` / `legacy_null_mode`。`auto` 路由记完整决策链；粘滞 / 手动只记 `final_agent + reason`；安全短路记 `reason='safety_blocked'` + 命中类型。否则审计回答不了"这条消息最终为什么用了 X Agent"（§13 决策 14）。

### 4.2 前端展示

- assistant 消息头部显示当前处理 Agent（如"🤖 用户管理助手"），从 `ai_message.agent_code` 取（§7.1b 新增列，确保历史消息也能还原）。
- `clarification_required` 事件 → 弹候选 Agent 卡片，用户点击后**前端自动重发原消息** + 选中的 `agentCode`（具体 code，无 confirmationId，详见 §6.2）。

----

## 5. 路由算法

### 5.1 LLM-only 路由（v4：砍掉规则阶段）

候选集 = 当前用户**有权限且已启用**的 Agent（`shared` 永远在候选集，作 catch-all）。

```python
async def route(deps: ChatDeps, message: str, candidates: list[AiAgent]) -> RouteResult:
    if not candidates:
        return RouteResult(failed=True, reason="no_candidates")  # → AI_ROUTING_FAILED

    provider = await provider_service.resolve_model(deps.db, None)  # 全局默认
    if provider is None:
        # 无 Provider 降级：deployment 未配 LLM，跳过路由直接 clarification
        return RouteResult(clarification=True, candidates=candidates,
                           reason="no_provider")

    prompt = build_router_prompt(candidates, message)
    try:
        raw = await provider.chat_complete(prompt, temperature=0.0, max_tokens=64)
    except Exception:
        return RouteResult(clarification=True, candidates=candidates,
                           reason="llm_call_failed")

    code = parse_agent_code_robustly(raw, candidates)  # 见下
    if code is None:
        return RouteResult(clarification=True, candidates=candidates,
                           reason="llm_unparsable_or_out_of_scope")

    return RouteResult(agent_code=code, reason="llm_resolved")
```

Prompt 模板（**Agent 列表动态从 DB 拉，不写死**）：

```
你是 HoHu AI 的 Agent 路由器。请根据用户问题，从以下 Agent 中选择最合适的一个。
仅返回 JSON（不要 markdown 代码块、不要解释）：{"agent_code": "..."}

可选 Agent（按 display_order）：
- shared（通用工具助手）: 文件解析、系统级统计等通用工具。当其它 Agent 都不合适时选这个。
- user_mgmt（用户管理助手）: ...（从 ai_agent 表动态拼接 name + description）
- ...

用户问题：{message}
```

**JSON 解析必须鲁棒**：
1. 先尝试整段 `json.loads`；
2. 失败则用正则截取首个 `{...}` 子串重试；
3. 仍失败、或解析出的 `agent_code` 不在候选集 → 视为失败（降级到 clarification）。

**Agent description 是路由准确率的唯一关键变量**——管理后台必须给每个 Agent 维护一段准确、区分度高的 description（建议 50-200 字，覆盖典型 query、与其他 Agent 的边界）。`scripts/seed_ai_agents.py` 给 7 个内置 Agent 写默认 description，详见 §7.3。

> **v4 砍规则阶段的理由**（详见 §13 决策 18 / §16 修订日志 R-1）：7 个 Agent LLM router 完全 hold 得住，strong/weak 关键词维护成本（碰撞调优 / 新增 Agent 回头审冲突）超过节省的 LLM 成本；小模型（haiku / 4o-mini / qwen-flash）单次 ~0.0001 美元、100-300ms，对内部 admin tool（用户量 < 100）远够。Agent 数量增长到 30+ 时再考虑 embedding 召回（§17 v2+ 演进）。

### 5.2 失败降级

- LLM 调用失败 / JSON 不解析 / `agent_code` 不在候选 → `clarification_required`（无状态 SSE event）。
- 无 Provider（部署未配 LLM，开源 TOB 默认状态）→ `clarification_required`，日志告警 `AI_SUPERVISOR_NO_PROVIDER`。
- 候选集空（用户无可用 Agent）→ `AI_AGENT_NOT_AVAILABLE`。
- 绝不静默随机选一个 Agent。
- 单次路由 LLM 失败**不重试**（避免双倍成本）；直接降级。

### 5.3 会话粘滞策略

`agentCode=null`（未传）时的决策树：
1. `ai_conversation.agent_code` 存在 → 直接复用，跳过 Supervisor（**避免会话中途切 Agent**）。
2. 新会话或上轮无 `agent_code` → 等价 `"auto"`，走 Supervisor。

`agentCode="auto"` 是**显式强制路由**：即使用户在和 `user_mgmt` 聊天，下一条消息也会重路由。给"我要换话题"的明确意图一个开关。

> **反例**（错误用法）：用户每轮都传 `null`，期望 Supervisor 每轮重路由 → 实际首轮之后全部粘滞到首轮 Agent。**正确做法**：要切话题就显式传 `"auto"` 或具体 code。

> **`routing_legacy_null_mode` 开关**（v4 新增，§15.3）：现有第三方 SDK / 老客户端依赖 `null` → `user_mgmt` 旧行为。部署方可在 `sys_config.ai:routing_legacy_null_mode=true` 时保持旧行为（跳过粘滞 + 跳过 Supervisor，直接走 `DEFAULT_AGENT_CODE`），平滑迁移。默认 `false`（新部署用新行为）。

----

## 6. API 改动

### 6.1 `POST /ai/chat` 的 `agentCode` 语义

| 值 | 行为 | 与现状对比 |
|---|---|---|
| 具体 agent code（如 `user_mgmt`） | 跳过路由，直接使用 | 不变 |
| `"auto"` | **新增**：强制走 Supervisor 路由 | 新语义 |
| `null` / 不传 | 会话粘滞（上轮 `agent_code`）→ 新会话 fallback `"auto"` | **变更**：现状 fallback `DEFAULT_AGENT_CODE = "user_mgmt"` |

> v4 砍掉原设计的 `confirmationId` 请求字段（详见 §6.2 / §13 决策 19）。

### 6.2 新增 SSE 事件 `clarification_required`（v4 无状态化）

```json
{
  "type": "clarification_required",
  "candidates": [
    {"code": "user_mgmt", "name": "用户管理助手", "description": "..."},
    {"code": "dept_mgmt", "name": "部门管理助手", "description": "..."}
  ],
  "message": "请问你想查询用户还是部门？"
}
```

**无状态 clarification 协议**（v4 简化）：
1. Supervisor 决策模糊 → emit `clarification_required`，**user 消息不落库**。
2. 前端本地暂存原 `messages` 数组（含图片 parts）+ `displayContent`，弹候选卡片。
3. 用户点击候选 → `POST /ai/chat` 带原 `messages` + 选中 `agentCode`（具体 code，非 `"auto"`）→ 后端按"显式 agentCode"路径处理（跳过路由）。
4. **防双击由前端 debounce 处理**（点击后立即 disable + loading），后端无状态。
5. **无 TTL 限制**：用户中途走开再回来点候选，依然能重发。如果届时上下文已变（新会话已建），由用户判断是否仍要发那条消息。
6. **`conversation_id` 可为 null**：新会话首条消息触发 clarification 时，前端暂存时无 `conversation_id`；用户选完 Agent 重发时，由 chat endpoint 按现有逻辑创建会话。

> **v4 砍 Redis confirmationId 的理由**（详见 §13 决策 19 / §16 R-2）：原协议（confirmationId + 5min TTL + Redis DEL 防重放 + 不存原消息防 PII）解决的是"用户重复点击"这个轻量问题，代码量 / 测试量跟问题严重度不匹配；成熟方案（ChatGPT / Claude inline clarification）就是无状态 SSE event + chip 重发。防双击用前端 debounce 就够了。

### 6.3 扩展 `GET /ai/agents`（**复用现有端点，不变**）

现状端点在 `hohu-admin/app/modules/ai/api/agent.py:25`，已支持：
- 超管：所有 `enabled=True` 的 Agent
- 普通用户：`role_ai_agent` 表关联 + `shared` 直通

本期**完全不变**——v4 砍掉 `routing_keywords` 列后，响应字段（`code` / `name` / `description` / `modelPreference` / `displayOrder`）刚好够 Supervisor 用。description 字段是路由准确率的关键变量，管理后台通过 Agent 管理页编辑 description 即可，无需新增字段。

```json
{
  "code": 200,
  "msg": "success",
  "data": [
    {
      "code": "shared",
      "name": "通用工具助手",
      "description": "...",
      "modelPreference": null,
      "displayOrder": 1
    }
  ]
}
```

- `shared` Agent 可见性无门槛，但其下的 tool 仍由 `compute_available_tools`（`app/modules/ai/agents/tools/registry.py:211`）按 `required_perms` 过滤，行为不变。
- **不采用 review 中"按 tool required_perms 过滤"的提议**：会改变 `shared` 之外所有 Agent 的可见性模型（破坏现有 RBAC）。继续用 `role_ai_agent` 表关联。

### 6.4 新增 `POST /ai/messages/{message_id}/routing-feedback`（v4 新增）

提交用户对某条 assistant 消息的路由反馈。

**请求**：
```json
POST /ai/messages/1234567890/routing-feedback
Authorization: Bearer <token>
Content-Type: application/json

{
  "feedback": "wrong",
  "correctedAgentCode": "dept_mgmt"
}
```

**字段约束**：
- `feedback: "correct" | "wrong"`（必填）
- `correctedAgentCode: str`（当 `feedback="wrong"` 时必填，必须是当前用户可见的 Agent code；`feedback="correct"` 时忽略此字段）

**响应**：`ResponseModel[null]`，成功 200。

**鉴权**：仅消息 owner 或超管可提交。非 owner → 403 `AI_AUTHORIZATION`。

**语义**：upsert——同一 message 重复提交覆盖最新 `corrected_agent_code`（允许用户改主意）。

**写入**：
- `ai_message.routing_feedback = 'wrong'`（或 `'correct'`）
- 追加 `ai_routing_feedback` 表一行（见 §7.1c）

**错误码**：
- `feedback='wrong'` 但缺 `correctedAgentCode` → 400 `AI_ROUTING_FEEDBACK_MISSING_CORRECTION`
- `correctedAgentCode` 不在用户可见 Agent → 403 `AI_AGENT_NOT_VISIBLE`
- 消息不存在 / 非 owner → 404 / 403

----

## 7. DB 迁移

### 7.1 表结构改动

**a) `ai_agent` 表：v4 不修改**

v4 砍掉原设计的 `routing_keywords` JSONB 列后，路由准确率完全由现有 `ai_agent.description` 字段决定（`description` 已存在，`app/modules/ai/models/agent.py:36`，Text nullable=False）。本期 migration **不动 ai_agent 表 schema**，仅通过 `scripts/seed_ai_agents.py` 更新 description 内容（详见 §7.3）。

> v3 原计划加 `routing_keywords JSONB` 列已在 v4 撤回（§13 决策 18 / §16 R-1）。

**b) `ai_message` 表：新增 `agent_code` + `routing_feedback` 列**

```python
from sqlalchemy import CheckConstraint

class AiMessage(Base):
    ...
    agent_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="本条消息实际处理的 Agent code"
    )
    routing_feedback: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment="用户路由反馈：'correct' / 'wrong' / null；用于 Supervisor 路由准确率监控 + description 调优"
    )

    __table_args__ = (
        CheckConstraint(
            "routing_feedback IS NULL OR routing_feedback IN ('correct', 'wrong')",
            name="ck_ai_message_routing_feedback"
        ),
    )
```

历史消息回填：`UPDATE ai_message m SET agent_code = c.agent_code FROM ai_conversation c WHERE m.conversation_id = c.conversation_id AND c.agent_code IS NOT NULL`。`routing_feedback` 不回填，默认 null。

`routing_feedback` 取值约束（DB 层 + 应用层双保险）：
- `correct`：用户主动点"选对了"（可选，UX 上不强制）。
- `wrong`：用户点"选错 Agent？"，前端弹下拉让用户选**正确**的 Agent → 同时追加 `ai_routing_feedback` 表（§7.1c）一行供运维 review。
- `null`：默认值，未反馈。

> **反馈收集不是实时调参**——首期仅做数据收集，定期（每周 / 每月）运维人工 review 错路由样本，调整 Agent description。**不做自动 feedback → 模型微调**，避免冷启动噪声。

**c) 新增 `ai_routing_feedback` 表（v4 新增）**

```python
class AiRoutingFeedback(Base):
    """用户对路由决策的反馈：每次点"选错 Agent？"或"选对了"追加一行。
    与 ai_message.routing_feedback 配合：后者是当前态（覆盖更新），
    本表是历史轨迹（append-only），便于运维按时间序列分析。
    """
    __tablename__ = "ai_routing_feedback"
    feedback_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=next_id)
    message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="关联 ai_message.message_id"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    original_agent: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="ai_message.agent_code，反馈时的原 Agent"
    )
    feedback: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="'correct' 或 'wrong'"
    )
    corrected_agent: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="feedback='wrong' 时用户选的正确 Agent code；'correct' 时为 NULL"
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="关联 ai_routing_log.trace_id，便于反查路由决策链"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    __table_args__ = (
        CheckConstraint(
            "feedback IN ('correct', 'wrong')",
            name="ck_ai_routing_feedback_type"
        ),
        CheckConstraint(
            "(feedback = 'wrong' AND corrected_agent IS NOT NULL) "
            "OR (feedback = 'correct' AND corrected_agent IS NULL)",
            name="ck_ai_routing_feedback_correction_match"
        ),
    )
```

**保留策略**：与 `ai_routing_log` 对齐保留 90 天（参照 `ai_operation_log` 现有策略）。`ai_message.routing_feedback` 是当前态（不随 feedback 表清理）。

### 7.2 新增 `ai_routing_log` 表

```python
class AiRoutingLog(Base):
    __tablename__ = "ai_routing_log"
    log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=next_id)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_message_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HMAC-SHA256(server_secret + user_id + message)；运维调试用，非法证取证",
    )
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False)
    llm_choice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="llm_resolved / clarification / session_sticky / manual_override / "
                "supervisor_disabled / safety_blocked / quota_exceeded / no_provider / "
                "no_candidates / legacy_null_mode",
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_log_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
        comment="v2+ 多 Agent 协作预留：fan-out 场景下指向触发本次路由的 log_id；首期始终为 NULL",
    )
    plan_step_index: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="v2+ 多 Agent 协作预留：本决策在 plan 中的步骤序号；首期始终为 NULL",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
```

> **v4 变更**：删 `rule_hits` 列（砍规则阶段）；加 `parent_log_id` / `plan_step_index` nullable 列为 v2+ 多 Agent 协作铺路（§13 决策 22 / §17）。`reason` 枚举去 `strong_unique` 加 `no_provider` / `no_candidates` / `legacy_null_mode`。

按月分区（v1.5+ 可加，首期单表）。**保留 90 天**，定时任务清理（参照 `ai_operation_log` 现有策略）。

### 7.3 迁移脚本要点

- `alembic revision --autogenerate -m "add ai_message.agent_code + ai_message.routing_feedback + ai_routing_log + ai_routing_feedback"`
- 手工补 `UPDATE` 语句（autogenerate 不会写数据回填）：
  ```sql
  UPDATE ai_message m
     SET agent_code = c.agent_code
    FROM ai_conversation c
   WHERE m.conversation_id = c.conversation_id
     AND c.agent_code IS NOT NULL;
  ```
  > `routing_feedback` 不回填，默认 NULL。
- `scripts/seed_ai_agents.py` 同步：**为每个内置 Agent 维护 50-200 字高区分度 description**（路由准确率的唯一关键变量）。description 应覆盖：
  - 该 Agent 的核心职责（一句话）
  - 典型 query 示例（2-3 个）
  - 与相邻 Agent 的边界（"X 类问题归本 Agent，Y 类问题归 Z Agent"）

  示例：

```python
AGENTS = [
    {
        "code": "shared",
        "name": "通用工具助手",
        "description": (
            "处理通用工具类请求：文件解析（Excel/CSV）、跨模块统计、不属于其他专用 Agent 的杂项。"
            "当用户问题不属于用户/角色/部门/任务/配置/Provider 任何专用领域时，选本 Agent。"
            "典型 query：'解析这个文件'、'统计系统的总体情况'。"
        ),
    },
    {
        "code": "user_mgmt",
        "name": "用户管理助手",
        "description": (
            "处理用户 CRUD、密码重置、账号解锁、用户状态变更、用户统计数据查询。"
            "典型 query：'重置 cs123 的密码'、'解锁已锁定的账号'、'统计启用的用户数'。"
            "边界：涉及角色/权限的归 role_mgmt；涉及部门归 dept_mgmt。"
        ),
    },
    {
        "code": "job_mgmt",
        "name": "定时任务管理助手",
        "description": (
            "处理定时任务（cron job）的查看、暂停、激活、cron 表达式修改、任务执行日志查询。"
            "典型 query：'修改 job_123 的 cron 为每天 8 点'、'暂停数据同步任务'。"
            "边界：一次性任务（非定时）归 shared。"
        ),
    },
    # ... 其余 Agent 同模式
]
```

> **description 维护责任**：每个 Agent 的 owner（业务方）负责 description 的准确性。`routing_feedback='wrong'` 数据每周 review，发现 description 不准及时调整。这是 v4 砍规则阶段后的关键运维动作。

----

## 8. 错误码

| 场景 | 错误码 / 事件 | 说明 |
|---|---|---|
| 用户没有任何可用 Agent | `AI_AGENT_NOT_AVAILABLE` | 提示管理员启用 Agent 或授权 |
| LLM 模糊 / 失败 / 无 Provider | `clarification_required` | SSE 事件，前端弹候选卡片（v4 无状态） |
| 选中的 Agent 被禁用或不存在 | `AI_AGENT_NOT_FOUND` | 返回 400 |
| LLM 分类器调用失败 | 降级 → `clarification_required` | 不给 500 |
| 路由决策异常（候选集空 / Supervisor 配额超限） | `AI_ROUTING_FAILED` | SSE 事件，前端提示"路由失败，请重试或手动选择 Agent" |
| 用户显式传 `agentCode` 但没权限 | 保持现有 `AI_AUTHORIZATION` / 403 | 不变 |
| `feedback='wrong'` 但缺 `correctedAgentCode` | `AI_ROUTING_FEEDBACK_MISSING_CORRECTION` | 返回 400 |
| `correctedAgentCode` 不在用户可见 Agent | `AI_AGENT_NOT_VISIBLE` | 返回 403 |
| 反馈目标 message 不存在 | `AI_MESSAGE_NOT_FOUND` | 返回 404 |
| 反馈提交者非 message owner 且非超管 | `AI_AUTHORIZATION` | 返回 403 |

----

## 9. 安全与配额

- **路由前复用既有安全检查**（`keyword_blocklist` / `forbidden_topics` / `forbidden_urls` / `injection_detector`）。现状代码在 `chat.py` 路由**之后**，本期重排到路由**之前**，同时修复"敏感词只能在路由后才拦截"的隐患。
- Supervisor 路由阶段**只读**，不操作业务数据；Supervisor LLM **不带任何 tool**。
- 最终执行 Agent 的 `risk_appetite` / `daily_quota_per_user` / `allowed_tools` 完全生效。
- Supervisor 只看用户有权限且已启用的 Agent，不会路由到无权限领域。
- **路由 LLM 调用单独计配额**（不与 PydanticAI 的 `UsageLimits` 混淆）：
  - 新增 `sys_config.ai:supervisor_daily_limit`（默认 100 次/用户/日）；
  - 超限时跳过 LLM 路由直接 emit `clarification_required`（接受更多 clarification），不让用户因 Supervisor 配额被阻塞。
- 路由 LLM 调用**不重试**（避免双倍成本）。
- **无 Provider 降级**：`provider_service.resolve_model(db, None)` 抛错或返回 None（部署未配置 LLM Provider，开源 TOB 默认状态）时，自动跳过 LLM 路由、直接 emit `clarification_required`；同时日志告警 `AI_SUPERVISOR_NO_PROVIDER`。避免新用户体验灾难（每次 auto 都 fallback clarification，至少让用户手动选）。

> **勘误 review 提到的 `usage_limits.request_limit`**：那是 PydanticAI Agent loop 内的兜底（`chat.py:434-437`，`request_limit=10 / tool_calls_limit=5`），在 Supervisor 路由**之前**就构造，物理上无法覆盖 Supervisor 的 LLM 调用。本期改用独立 `sys_config.ai:supervisor_daily_limit`。

> **性能开销**：每次 `"auto"` 路由相对现状额外增加：1 次 DB 查询（拉用户可用 Agent）+ 1 次 DB 写（`ai_routing_log`）+ 1 次 LLM 调用（LLM 阶段，100–800ms 取决于模型）。粘滞 / 手动请求只增加 1 次 DB 写。**v4 砍 Redis clarification 后无 Redis 开销**（仅 Supervisor 配额 L2 计数复用现有 Redis key 模式，参照 `ai_operation_log`）。

----

## 10. 前端改动

- `chat-input.vue` 的 Agent 下拉框新增 **"自动"**（值 `"auto"`）和 **"沿用上轮"**（值 `null` / 不传，默认）两个选项。
- 发送消息时，`agentCode` 传当前选择值。
- assistant 消息头部显示当前处理 Agent 名称（从 `ai_message.agent_code` 取，§7.1b）。
- 处理 `clarification_required` 事件（v4 无状态化）：
  - 弹候选 Agent 卡片；
  - 用户点击 → **前端 debounce（点击立即 disable + loading）** → 自动重发原 `messages` 数组（含图片 parts）+ 原 `displayContent` + 选中 `agentCode`（具体 code）；
  - 无 `confirmationId`、无 TTL 倒计时——用户走开再回来仍可点。
- **路由反馈按钮**（v4 新增）：每条 assistant 消息底部加"选错 Agent？"轻量按钮：
  - 点击 → 弹下拉让用户选**正确**的 Agent（候选来自当前可见 Agent 列表）；
  - 提交 → `POST /ai/messages/{id}/routing-feedback` body `{feedback: "wrong", correctedAgentCode: "..."}`；
  - 后端写 `ai_message.routing_feedback='wrong'` + 追加 `ai_routing_feedback` 表；
  - 提交后按钮变灰 disabled + 显示"已反馈，感谢"。
- **可选 `correct` 反馈**：UX 上不强制做"选对了"按钮（噪声大、用户懒得点）；首选"被动收集"（默认 null，只在错时主动反馈）。

### 10.1 管理后台（⚠️ Plan supervisor-routing gap，首期可不做）

- Agent 管理页需扩展 `description` 编辑入口（**多行 textarea，建议 50-200 字**）+ 实时预览路由影响（"测试 query" 输入框 → 调 `/ai/routing-test` 返回 LLM 选择，便于调优）。
- 首期可通过 `scripts/seed_ai_agents.py` + SQL 维护；管理后台 UI 标 `⚠️ Plan supervisor-routing gap`，后续补。
- **路由反馈仪表盘**：展示近 7 / 30 天 `routing_feedback='wrong'` 的样本数 / 错路由 matrix（original → corrected），辅助 description 调优。

----

## 11. 测试策略

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/modules/ai/agents/supervisor/test_router.py` | LLM 唯一解析成功、JSON 鲁棒解析（markdown 包裹 / prose 包裹 / 字段缺失 / code 不在候选）、`shared` 作为 catch-all、权限过滤、禁用 Agent 过滤、LLM 调用异常降级、无 Provider 降级、候选集空抛 `AI_ROUTING_FAILED` |
| `tests/modules/ai/agents/supervisor/test_session_stickiness.py` | `agentCode=null` 粘滞上轮、新会话 fallback `auto`、`auto` 强制重路由、显式 code 覆盖粘滞、**粘滞的 Agent 已被禁用 → fallback `auto`**、**`routing_legacy_null_mode=true` 时 `null` 走 `DEFAULT_AGENT_CODE` 旧行为** |
| `tests/modules/ai/agents/supervisor/test_safety_order.py` | keyword / topic / injection 命中时**不进入路由**、路由前置后安全层仍生效、**敏感词命中不产生孤儿 user 消息（§13 决策 13）** |
| `tests/modules/ai/agents/supervisor/test_clarification.py` | 原消息不落库、emit clarification event 后流正常结束、用户点击重发链路（带显式 agentCode 跳过路由）、**`conversation_id=null` 新会话流程**、**`displayContent` / `parts` 保留**、**前端 debounce 防双击（前端单测）** |
| `tests/modules/ai/agents/supervisor/test_routing_audit.py` | `ai_routing_log` 字段完整、`input_message_hash` 不存明文（HMAC）、按 `trace_id` 关联 `ai_operation_log`、**所有请求类型都被记录**（auto / sticky / manual / safety_blocked / no_provider / legacy_null_mode）、`parent_log_id` / `plan_step_index` 首期始终 NULL、保留期清理 |
| `tests/modules/ai/agents/supervisor/test_routing_feedback.py`（v4 新增） | `POST /ai/messages/{id}/routing-feedback` 写入 `ai_message.routing_feedback` + `ai_routing_feedback` 表、权限校验（仅消息 owner 或超管）、重复反馈 upsert（覆盖最新 corrected_agent + 追加 feedback 历史）、`routing_feedback='wrong'` 但未提供 `correctedAgentCode` → 400、`correctedAgentCode` 不在可见 Agent → 403、`feedback` 非 `correct`/`wrong` → 400（CHECK 约束） |
| `tests/modules/ai/test_chat_supervisor.py` | 显式 `agentCode` 行为不变、`auto` 命中返回对应 Agent tool 调用、模糊 query 返回 clarification 事件、无可用 Agent 返回错误、Supervisor 配额超限降级、**无 Provider 时降级到 clarification**、**Supervisor 关闭 fallback `DEFAULT_AGENT_CODE`**、**`supervisor_daily_limit` 并发竞态**、**`routing_legacy_null_mode=true` 时 `null` 不进粘滞也不进 Supervisor** |
| 迁移测试 | `ai_message.agent_code` 回填、`routing_feedback` 默认 NULL、`ai_routing_log` 新表、旧数据（含 `agent_code=NULL` 会话）不报错 |
| 手动测试 | 前端"自动/沿用上轮"选项、Agent 标签、clarification 卡片无 TTL 倒计时、"选错 Agent？"反馈按钮、`corrected_agent_code` 下拉选择 |

> 测试隔离遵循 `CLAUDE.md` 跨项目硬规则 #7：每测试自清残留、不依赖 `created_at DESC` 排序（用显式 version/id）。v4 砍 Redis confirmation 后**无 Redis 相关测试**（仅 Supervisor 配额 / 路由 LLM 调用相关 Redis 测试保留，参照 `ai_operation_log` 现有策略）。

----

## 12. 实现边界

**In scope**：
- 单次路由 + `"auto"` 模式 + 会话粘滞
- **LLM-only 路由**（v4 砍规则阶段，§5.1）
- `ai_message.agent_code` + `ai_message.routing_feedback` 列 + agent_code 回填
- `ai_routing_log` 表 + 写入（含 `parent_log_id` / `plan_step_index` nullable 扩展位）
- `ai_routing_feedback` 表 + 写入（v4 新增，反馈历史轨迹）
- `clarification_required` SSE 事件（v4 无状态，无 Redis confirmation）
- `POST /ai/messages/{id}/routing-feedback` 端点（v4 新增）
- 前端 Agent 选择器加"自动/沿用上轮" + Agent 名称展示 + "选错 Agent？"反馈按钮
- 当前用户可用 Agent 过滤（复用 `GET /ai/agents`，端点本身不变）
- 安全检查前置（路由前）
- `sys_config.ai:supervisor_enabled` / `ai:supervisor_daily_limit` / `ai:routing_legacy_null_mode` 开关
- `scripts/seed_ai_agents.py` 更新 7 个内置 Agent 的 50-200 字高区分度 description

**Out of scope**：
- 一轮内多 Agent 协作（§17.2 v2+ 演进）
- Agent marketplace / 插件机制
- 自定义 Supervisor 模型
- 基于多轮历史的上下文感知路由
- Embedding 召回（§17.1，Agent > 30 时再考虑）
- 路由准确率 SLO + 自动告警（§17.4）
- 自动 description 调优（§17.5）

----

## 13. 决策记录

1. **Supervisor 只做单次路由** — 降低实现复杂度和审计难度，v1.5+ 先不实现多 Agent 协作。**反例**: 让 Agent A 调 Agent B → 状态机爆炸、审计链断裂、HITL 难以归属。**回归**: `test_router.py::test_no_agent_chain`。
2. ~~**路由关键词分 strong/weak 两层**~~ **OBSOLETED by 决策 18（v4）**：砍规则阶段后不再需要关键词维护；路由准确率由 `ai_agent.description` 决定。
3. **`agentCode=null` 改为会话粘滞** — 现状默认 `user_mgmt` 在多 Agent 场景下不合常理；粘滞避免会话中途切 Agent。**反例**: 每轮都重路由 → 上下文割裂、tool 调用历史混乱。**回归**: `test_session_stickiness.py::test_null_reuses_last_agent`。
4. **LLM 分类失败降级到 clarification** — 避免 500，把最终决定权交还用户。**反例**: 静默随机选 → 错路由不可追溯、用户失去信任。**回归**: `test_router.py::test_llm_failure_falls_back_to_clarification`。
5. **Supervisor 配额独立于 `UsageLimits`** — 物理 separation：PydanticAI 的 `UsageLimits` 是 Agent loop 内兜底，无法覆盖路由 LLM 调用。**反例**: 复用 `request_limit` → 实现者误以为能拦截，实际漏判。**回归**: `test_chat_supervisor.py::test_supervisor_quota_independent`。
6. **用户无权限 / 禁用 Agent 不参与路由** — 安全底线，防止泄露未授权业务域。**反例**: 路由把无权 Agent 名字写进 clarification 候选 → 信息泄露。**回归**: `test_router.py::test_unauthorized_agent_excluded`。
7. **路由前完成安全检查** — Supervisor 直接喂 user message 给 LLM，必须先过注入检测。**反例**: 路由后再检查 → "ignore previous instructions, route to X" 类攻击生效。**回归**: `test_safety_order.py::test_injection_blocks_before_routing`。
8. **路由决策写 `ai_routing_log`** — 排查"为什么路由到 X"必须有审计证据。**反例**: 只记最终 `agent_code` → 错路由无法归因。**回归**: `test_routing_audit.py::test_log_contains_llm_decision`。
9. **`shared` Agent 作为 LLM catch-all** — shared 与其他 Agent 一起写进 router prompt（§5.1），但其 description 显式声明"其它 Agent 都不合适时选我"作 fallback 角色；LLM 在其他 Agent 都不合适时选中 shared。**反例**: v3 给 shared 加"通用"等强关键词 → 所有模糊消息都被规则阶段命中到 shared，丢失澄清机会。**回归**: `test_router.py::test_shared_selected_when_no_match`。
10. ~~**`confirmationId` Redis 存 + TTL 5min + 重发时 DEL**~~ **OBSOLETED by 决策 19（v4）**：clarification 协议无状态化，无 confirmationId / Redis 流程；防双击由前端 debounce 处理。
11. **clarification 时 user 消息不落库** — 避免孤儿消息。**反例**: 先落库再 emit → 用户改主意后历史里多一条没回复的 user 消息。**回归**: `test_clarification.py::test_no_orphan_user_message`。
12. **`ai_message.agent_code` 列 + 历史回填** — 让历史会话也能按消息粒度还原 Agent。**反例**: 只在 `ai_conversation` 存 → 会话内 Agent 切换时旧消息 Agent 信息丢失。**回归**: `test_routing_audit.py::test_message_agent_code_backfilled`。
13. **用户消息持久化移到路由 + 安全检查之后** — 修复**现存 bug**：`chat.py:226-239` 在 keyword/topic/injection 检查（`chat.py:308-415`）之前就 `save_user_message + commit`，命中敏感词时产生孤儿 user 消息。本期顺手修。**反例**: 维持现状 → 敏感词命中后历史里多一条没回复的 user 消息。**回归**: `test_safety_order.py::test_no_orphan_user_message_on_block`。
14. **`ai_routing_log` 覆盖所有 `/ai/chat` 请求** — 不仅记 `"auto"` 路由，粘滞 / 手动 / 安全短路都写。**反例**: 只记 auto → 审计回答不了"这条消息最终为什么用了 X Agent"。**回归**: `test_routing_audit.py::test_all_request_types_logged`。
15. **`DEFAULT_AGENT_CODE = "user_mgmt"` 常量保留** — 作为粘滞失效 / Supervisor 关闭 / 无 Provider 降级 / `routing_legacy_null_mode=true` 的终极 fallback。新增 stickiness 逻辑**包裹**它，不替换。**反例**: 删常量改默认值 → rollback 开关（§15.3）失去 fallback 目标。**回归**: `test_chat_supervisor.py::test_supervisor_disabled_falls_back_to_constant`。
16. ~~**Redis 不存 `original_message`**~~ **OBSOLETED by 决策 19（v4）**：整个 Redis clarification 流程都砍了，本决策无意义。
17. **`input_message_hash` 用 HMAC-SHA256 而非裸 SHA256** — 防彩虹表反查、防跨用户频率分析。`hmac_sha256(server_secret + user_id + message)`。**反例**: 裸 SHA256 → "查询用户列表" 等常见明文易被反查。**回归**: `test_routing_audit.py::test_hash_is_hmac_not_plain`。
18. **v4 砍规则阶段，LLM-only 路由** — 7 个 Agent LLM router 完全 hold 得住，strong/weak 关键词维护成本（碰撞调优 / 新增 Agent 回头审冲突）超过节省的 LLM 成本；小模型（haiku / 4o-mini / qwen-flash）单次 ~0.0001 美元、100-300ms，对内部 admin tool 远够。**反例**: 维持规则阶段 → 7 个 Agent 已经要分 strong/weak 两层防碰撞，到 20 个会失控；新增 Agent 要回头审所有现有关键词违反开闭原则。**回归**: `test_router.py::test_pure_llm_routing_no_keywords`。
19. **v4 clarification 协议无状态化（砍 Redis confirmationId）** — 原协议（confirmationId + 5min TTL + Redis DEL 防重放 + 不存原消息防 PII）解决的是"用户重复点击"这个轻量问题，代码量 / 测试量（8 个子 case）跟问题严重度不匹配；成熟方案（ChatGPT / Claude inline clarification）就是无状态 SSE event + chip 重发。**反例**: 维持 Redis 协议 → 多一组 Redis key schema、多 5 个测试 case、多 TTL/重放边界条件维护，零业务价值。**回归**: `test_clarification.py::test_stateless_resend`。
20. **v4 加 `ai_message.routing_feedback` + 反馈收集表** — 路由准确率必须有反馈闭环；否则 LLM-only 路由调优没数据支撑，运维不知道哪些 query 错路由。首期仅数据收集，不做自动调参。**反例**: 不加反馈 → 永远不知道路由准确率，description 调全凭感觉。**回归**: `test_routing_feedback.py::test_feedback_recorded`。
21. **v4 加 `sys_config.ai:routing_legacy_null_mode` 开关** — `null` 语义从"fallback user_mgmt"变成"粘滞 + auto"是 silent breaking change，第三方 SDK / 老客户端不会知道；开关默认 `false`（新部署新行为），TOB 升级时可设 `true` 保持兼容。**反例**: 强制 breaking change → 老客户端静默走粘滞逻辑，行为不可预期。**回归**: `test_chat_supervisor.py::test_legacy_null_mode_uses_default_agent_code`。
22. **v4 `ai_routing_log` 留 `parent_log_id` / `plan_step_index` 扩展位** — 多 Agent 协作（一个 query 触发多 Agent 并行或串行）是明确未来方向（§17）；首期始终 NULL，但 schema 留位避免后期 ALTER 影响线上。**反例**: 不留位 → 未来支持多 Agent 协作时需 ALTER 大表 + 回填 NULL，影响线上稳定性。**回归**: `test_routing_audit.py::test_fanout_fields_nullable_by_default`。

----

## 14. 参考

- `hohu-admin/docs/specs/2026-07-02-ai-tool-gateway-design.md` §5.4 / §14
- `hohu-admin/app/modules/ai/models/agent.py`
- `hohu-admin/app/modules/ai/api/chat.py`（路由前安全检查改造点：`chat.py:226-415`）
- `hohu-admin/app/modules/ai/api/agent.py:25`（**已存在**的 `GET /ai/agents`）
- `hohu-admin/app/modules/ai/service/chat_service.py:26,129`（`DEFAULT_AGENT_CODE = "user_mgmt"` 常量**保留**；新增 stickiness 逻辑包裹它，详见 §13 决策 15）
- `hohu-admin/scripts/seed_ai_agents.py:29`（7 个内置 Agent，需维护 50-200 字高区分度 description，详见 §7.3）
- `hohu-admin-web/src/views/ai/chat/modules/chat-input.vue`

----

## 15. 兼容性与上线策略

### 15.1 客户端影响

- **Web**：现有调用默认未传 `agentCode`，将从"默认 user_mgmt"变成"会话粘滞 + auto"。需要前端升级到带"自动/沿用上轮"选项的版本。
- **移动端 / 桌面端**：同上，需检查 `chat-input` 组件是否同步。
- **SDK / 第三方调用**：如显式传具体 code（如 `user_mgmt`），行为完全不变。
- **`null` 语义变更**（v4 强调）：TOB 客户升级前可在后端设 `sys_config.ai:routing_legacy_null_mode=true` 保持旧行为，避免老客户端静默走粘滞。

### 15.2 上线步骤

1. 后端发布：DB 迁移 + Supervisor 模块 + 路由前置安全检查；旧前端继续工作（`null` → 粘滞，首轮 fallback `auto`，Supervisor 接管）。
2. 前端发布：Agent 下拉框新增"自动/沿用上轮"选项 + clarification 卡片 + 反馈按钮。
3. 灰度：先在 dev / staging 验证 `ai_routing_log` 决策分布，调优 Agent description（v4 不再调关键词）。
4. 全量。

### 15.3 回滚

- DB 迁移向后兼容（仅加列 / 加表），无需回滚 schema。
- 后端开关 `sys_config.ai:supervisor_enabled`（默认 `true`）。关闭后：粘滞逻辑跳过、`"auto"` 不进路由、所有请求最终走 `DEFAULT_AGENT_CODE = "user_mgmt"`（旧行为）。**常量本身不删**（§13 决策 15），只是绕过新增的 stickiness + Supervisor 代码路径。
- 前端隐藏"自动"选项，强制用户手选。
- **`sys_config.ai:routing_legacy_null_mode`（v4 新增，默认 `false`）**：单独的"旧行为"开关，比 `supervisor_enabled` 更细粒度——保留 Supervisor 给 `"auto"` 用，但 `null` 走 `DEFAULT_AGENT_CODE` 不进粘滞。用于：TOB 客户端不升级但后端想用 Supervisor 的过渡期。

----

## 16. 修订日志（v3 → v4，2026-07-24）

> **修订背景**：v3 review 时对标市面成熟方案（OpenAI Swarm / LangGraph / AutoGen / CrewAI / Coze / Dify / ChatGPT / Claude + tool_use），发现 v3 在两处过度设计（规则阶段、Redis clarification），并缺反馈闭环。v4 收敛到"LLM-only router + 无状态 clarification + 反馈闭环"——这是业界对 7 个 Agent 规模的主流模式。

### 修订总表

| ID | 范围 | 修订位置 | motivation |
|---|---|---|---|
| **R-1** | 砍规则阶段（strong/weak keyword） | §3 / §4.1.3 / §5.1 / §7.1a / §7.3 / §13 决策 2 → 18 | 关键词碰撞维护成本 > 节省的 LLM 成本；7 个 Agent LLM router 完全 hold 得住；新增 Agent 违反开闭原则 |
| **R-2** | 砍 Redis confirmationId 流程 | §4.1.4 / §6.1 / §6.2 / §13 决策 10 → 19 / §13 决策 16 OBSOLETED | 解决"用户重复点击"用前端 debounce 足够；成熟方案（ChatGPT/Claude）都是无状态 SSE event + chip 重发；Redis key schema + TTL + 重放边界维护成本零业务价值 |
| **R-3** | 加 `ai_message.routing_feedback` + 反馈收集表 | §7.1b / §7.1c / §10 / §11 / §13 决策 20 | LLM-only 路由必须配反馈闭环，否则 description 调优没数据支撑 |
| **R-4** | 加 `sys_config.ai:routing_legacy_null_mode` 开关 | §5.3 / §13 决策 21 / §15.1 / §15.3 | `null` 语义变更是 silent breaking change，第三方 SDK 不会知道；开关保护 TOB 升级 |
| **R-5** | `ai_routing_log` 加 `parent_log_id` / `plan_step_index` 扩展位 | §7.2 / §13 决策 22 | 为 v2+ 多 Agent 协作铺路（§17）；首期 NULL，schema 留位避免后期 ALTER |
| **R-6** | `ai_routing_log.reason` 枚举调整 | §7.2 / §4.1.6 | 删 `strong_unique`（砍规则）；加 `no_provider` / `no_candidates` / `legacy_null_mode` |
| **R-7** | `shared` Agent 路由定位调整 | §13 决策 9 | 不再"无 strong 关键词规则阶段永不命中"，改为"description 写明 fallback 角色 + LLM 选" |
| **R-8** | v3 残留文本清理（二次 review 发现） | §2 / §4.2 / §6.3 / §7.1a / §7.1b / §7.1c / §8 / §9 / §11 / §12 / §13 决策 9 | v4 首轮修订漏改 10+ 处 v3 文本（仍引用 `confirmationId` / `routingKeywords` / "规则阶段"等已删概念）；二次 review 系统清理。同时补：`routing_feedback` CHECK 约束、`ai_routing_feedback` 正式表定义（原仅文字描述）、`POST /ai/messages/{id}/routing-feedback` 端点正式定义（原仅 §10 引用）、§7.1a 表述清理（v4 不动 ai_agent 表） |

### 不改的部分（仍然有效）

- §3 整体架构（Supervisor 单次路由 + 粘滞 + `"auto"` 开关）
- §4.1 安全检查前置（顺带修孤儿消息 bug）
- §4.1 第 6 步审计覆盖所有请求类型
- §5.3 会话粘滞策略本身
- §6.1 `agentCode` 三种语义（除 `confirmationId` 字段删除）
- §9 配额独立 / 无 Provider 降级 / Supervisor 总开关
- §13 决策 1 / 3 / 4 / 5 / 6 / 7 / 8 / 11 / 12 / 13 / 14 / 15 / 17（13 条仍然有效）

### 测试净变化

- 删：~12 个 test case（rule stage / Redis confirmation / TTL / 重放防护相关）
- 加：~8 个 test case（LLM-only router 边界 / 反馈 upsert + 校验 / `legacy_null_mode` / fan-out 字段默认 NULL）
- 净减 ~4 个 test case，但路由准确率可观测性大幅提升（反馈数据）

### 代码量预估

- v3 实现约 1500 行（含 Redis confirmation + 关键词 + 规则阶段）
- v4 实现约 900 行（砍 Redis schema + 砍规则阶段，加反馈表 + legacy 开关）
- 减约 40% 代码量，测试覆盖更聚焦

----

## 17. v2+ 演进方向（Plan gap）

> 本节是 **未来方向记录**，本期 v1.5+ **不做**。避免后期重复设计 + 给当前 schema 决策提供 future-proofing 依据。

### 17.1 ⚠️ Embedding 召回（当 Agent > 30 时启用）

**触发条件**：内置 + 自定义 Agent 总数超过 30，LLM router 准确率下降 / 延迟上升。

**方案**：
- Agent description 向量化（OpenAI text-embedding-3-small / BGE-m3 / m3e-base）
- query 来时算 query embedding，cosine similarity top-K（K=5）
- LLM 在 top-K 里选最终 agent_code

**基础设施**：
- 优先用 PostgreSQL `pgvector` 扩展（无新依赖）
- `ai_agent` 表加 `description_embedding vector(1536)` 列（pgvector）
- Agent description 变更时异步重算 embedding（参照 `ai_agent.update_time` 增量）

**演进路径**：v4 设计的 `ai_routing_log.candidates` 字段已经支持"召回子集"语义；加 embedding 时只改 `AgentRouter.route` 内部，外部 API 不变。

### 17.2 ⚠️ 多 Agent 协作（fan-out / plan-and-execute）

**触发条件**：用户 query 明确跨域（"统计用户和部门数量并对比"），单 Agent 无法完成。

**方案**：
- 引入 `PlanNode` 概念：LLM 把 query 拆成多个 step，每个 step 路由到一个 Agent
- `ai_routing_log.parent_log_id` + `plan_step_index` 已在 v4 schema 留位
- 执行模式：并行（fan-out 同时调多 Agent）或串行（plan-and-execute 按依赖图）
- HITL 归属：每个 step 独立计配额 / 独立 HITL，但 audit 通过 `parent_log_id` 串联

**参考**：LangGraph state graph / AutoGen GroupChat / CrewAI hierarchical process

**风险**：状态机复杂度爆炸；HITL 归属混乱；审计链断裂。v2+ 设计要独立 spec，不能塞 v1.5+。

### 17.3 ⚠️ 单 Agent + 动态 tool 子集（替代方案评估）

**问题**：业界默认是单 Agent + 多 tool（ChatGPT GPTs / Claude / Bedrock Agents / Vertex AI Agent Builder）。多 Agent 路由的真正动机是什么？

**hohu 多 Agent 的合理理由**：
- 不同 Agent 要不同 system prompt / 模型（✓ job_mgmt 要 cron 知识）
- 不同 risk_appetite / 配额（✓ SR-21 per-agent risk_appetite）
- 上下文隔离（避免 tool 列表污染推理）

**单 Agent + 动态 tool 替代方案**：
- 一个总 Agent，包含所有 tool
- LLM 推理时按 query 动态 expose 相关 tool 子集（tool retrieval）
- 等同于 "Agent routing" 但 routing 发生在 tool 层而非 Agent 层

**对比**：

| 维度 | 多 Agent 路由（当前 v4） | 单 Agent + 动态 tool |
|---|---|---|
| Routing 颗粒度 | Agent 级（粗） | Tool 级（细） |
| system prompt 隔离 | 强（每 Agent 独立 prompt） | 弱（共享 prompt） |
| risk_appetite 隔离 | 强 | 弱（需 tool 级 risk） |
| Tool 上下文污染 | 无 | 有（即使动态筛选，仍可能 expose 不相关 tool） |
| 实现复杂度 | 中（v4 ~900 行） | 高（tool retrieval + tool 级 risk） |
| 业界案例 | AutoGen / CrewAI / Coze | ChatGPT / Claude / Bedrock |

**结论**：hohu 的多 Agent 动机成立（risk_appetite + system prompt 隔离是硬需求），v4 多 Agent 路由是合理选择。**但 v2+ 应评估混合方案**：核心域（user/role/dept）用多 Agent，跨域工具（file.parse）用单 Agent shared。

### 17.4 ⚠️ 路由准确率 SLO + 自动告警

**触发条件**：生产环境路由量大到需要 SLO（> 1000 次/日）。

**方案**：
- 基于 `ai_routing_feedback` 计算错路由率（wrong / total）
- Prometheus 指标 `ai_routing_wrong_rate` + 告警阈值（> 10% 触发告警）
- 配合 v1.5+ Prometheus 接入（hohu-admin commit `7ea6e8f`，hohu-cli monitoring 待做）

### 17.5 ⚠️ 自动 description 调优

**触发条件**：积累 1000+ wrong feedback 样本。

**方案**：
- LLM batch job：每周 review 错路由样本 + description，自动建议修订
- 人工 review 后合并（不直接自动 apply）
- 与 §17.4 SLO 联动：错路由率 > 阈值时触发 batch job

**不做**：实时 feedback → 模型微调（冷启动噪声大，运维复杂度高）。
