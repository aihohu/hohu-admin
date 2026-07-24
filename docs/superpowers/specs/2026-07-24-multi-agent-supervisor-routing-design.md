# Multi-Agent Supervisor 路由设计

**Status**: ⚠️ Plan draft（awaiting review）  
**Created**: 2026-07-24  
**Owner**: hohu core team  
**Depends on**: spec `2026-07-02-ai-tool-gateway-design.md` §5.4 / §14 / §17.2；`hohu-admin` 已存在 `ai_agent` 表、Agent 选择器、`@ai_tool(agent=...)` 分组。

----

## 1. 背景与目标

`hohu-admin` 当前已内置 5+ 业务 Agent（`shared`、`user_mgmt`、`role_mgmt`、`dept_mgmt`、`job_mgmt` 等），前端已支持手动选择 Agent。但用户需要**先选对 Agent 再问问题**，体验割裂。

本设计目标：
- 用户发送消息时，Supervisor 自动选择最合适的 Agent。
- 保留手动选择能力作为覆盖（override）。
- 模糊时通过 clarification 卡片让用户确认，而不是瞎猜。
- 不破坏现有单 Agent 执行路径、安全、审计和配额体系。

----

## 2. 非目标

本期**不做**：
- 一轮对话内多 Agent 协作（Agent A 调完再调 Agent B）。
- 会话中途自动切换 Agent。
- Agent marketplace / 外部插件动态注册 Agent。
- 基于历史上下文的复杂路由（只用当前消息）。
- 自定义 Supervisor 模型（先用默认模型，后续通过 `sys_config` 扩展）。

----

## 3. 架构

新增 `app/modules/ai/agents/supervisor/` 模块，职责单一：**根据用户消息选 Agent**。

```
用户输入
  │
  ▼
POST /ai/chat
  │
  ├── agentCode 显式指定（如 user_mgmt） ──► 直接用该 Agent（现有行为）
  │
  └── agentCode = null / "auto" ──► AgentRouter.route(deps, message)
                                      │
                                      ├── 规则命中唯一 Agent ──► 返回 agent_code
                                      ├── 多命中 / 无命中 ─────► LLMClassifier
                                      └── 仍模糊 ─────────────► clarification_required
  │
  ▼
chat_service.build_chat_deps(agent_code=...)
  │
  ▼
create_agent(agent_code) → 原有单 Agent 执行流（HITL / quota / audit 不变）
```

核心原则：**Supervisor 只替换“选 Agent”这一步**，不侵入 tool executor、HITL、SSE 流式逻辑。

----

## 4. 数据流

1. **前端发消息**  
   `POST /ai/chat` body 中 `agentCode` 可传具体 code、`"auto"` 或不传（不传默认等价于 `"auto"`）。

2. **后端路由**  
   - `AgentRouter` 从 `ai_agent` 表加载当前用户**有权限且已启用**的 Agent。
   - 先用 `routing_keywords` 做规则匹配：
     - 唯一命中 → 返回该 `agent_code`。
     - 多命中 / 无命中 → 走 `LLMClassifier`。
   - `LLMClassifier` 根据 Agent 的 `name`、`description`、`routing_keywords` 做选择；
     - 若 top1 与 top2 置信度差距过小 → 返回 `clarification_required`。

3. **执行**  
   用选中的 `agent_code` 走 `build_chat_deps` → `create_agent` → stream。最终 `agent_code` 写入 `ai_conversation.agent_code` 和 `ai_operation_log`。

4. **前端展示**  
   - assistant 消息头部显示当前处理 Agent（如“🤖 用户管理助手”）。
   - 若返回 `clarification_required`，前端展示候选 Agent 卡片，用户点击后继续。

----

## 5. 路由算法

### 5.1 规则阶段

每个 Agent 维护 `routing_keywords: list[str]`。

```python
def rule_route(agents: list[AiAgent], message: str) -> RouteResult:
    lowered = message.lower()
    hits = [a for a in agents if any(k in lowered for k in a.routing_keywords)]
    if len(hits) == 1:
        return RouteResult(agent_code=hits[0].code)
    return RouteResult(ambiguous=True, candidates=hits or agents)
```

- 命中唯一：直接返回。
- 命中多个或无命中：把候选集交给 LLM。

### 5.2 LLM 阶段

Prompt 模板（示例）：

```
你是 HoHu AI 的 Agent 路由器。请根据用户问题，从以下 Agent 中选择最合适的一个。
仅返回 JSON：{"agent_code": "...", "confidence": 0.95}

可选 Agent：
- shared（通用工具助手）: 文件解析、系统级统计等通用工具
- user_mgmt（用户管理助手）: 用户/部门/角色查询与维护
- role_mgmt（角色权限助手）: 角色 CRUD、菜单绑定
- dept_mgmt（部门管理助手）: 部门树查询/维护
- job_mgmt（定时任务助手）: 定时任务查询/启停/改 cron

用户问题：{message}
```

- 使用默认 LLM 模型（后续可加 `sys_config.ai:supervisor_model`）。
- `confidence < 0.7` 或 top1 - top2 < 0.2 → 返回 `clarification_required`。

### 5.3 失败降级

- LLM 调用失败 → 回退到规则阶段的结果；规则也无法唯一命中 → `clarification_required`。
- 绝不静默随机选一个 Agent。

----

## 6. API 改动

### 6.1 `POST /ai/chat`

请求 body 中 `agentCode` 语义扩展：

| 值 | 行为 |
|---|---|
| 具体 agent code | 保持现有行为，直接使用 |
| `"auto"` 或 `null` / 不传 | 走 Supervisor 自动路由 |

### 6.2 新增 SSE 事件 `clarification_required`

```json
{
  "type": "clarification_required",
  "confirmationId": "cf_xxx",
  "candidates": [
    {"code": "user_mgmt", "name": "用户管理助手", "description": "..."},
    {"code": "dept_mgmt", "name": "部门管理助手", "description": "..."}
  ],
  "message": "请问你想查询用户还是部门？"
}
```

前端收到后弹出候选卡片，用户选择后发送下一条带 `agentCode` 的消息。

### 6.3 可选新增 `GET /ai/agents`

返回当前用户**有权限且已启用**的 Agent 列表，供前端下拉框和 clarification 卡片使用。若前端 store 已具备该能力，可复用。

----

## 7. DB 迁移

给 `ai_agent` 表新增一列：

```python
from sqlalchemy.dialects.postgresql import JSONB

class AiAgent(Base):
    ...
    routing_keywords: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Supervisor 规则路由关键词",
    )
```

- 已有数据迁移时填充空数组 `[]`。
- 同步更新 `scripts/seed_ai_agents.py`，为每个内置 Agent 写入默认关键词。

示例：

```python
{
    "code": "job_mgmt",
    "name": "定时任务助手",
    "routing_keywords": ["定时任务", "cron", "调度", "job", "定时器"],
}
```

----

## 8. 错误码

| 场景 | 错误码 / 事件 | 说明 |
|---|---|---|
| 用户没有任何可用 Agent | `AI_AGENT_NOT_AVAILABLE` | 提示管理员启用 Agent 或授权 |
| 规则多命中 / LLM 模糊 | `clarification_required` | SSE 事件，前端弹候选卡片 |
| 选中的 Agent 被禁用或不存在 | `AI_AGENT_NOT_FOUND` | 返回 400 |
| LLM 分类器调用失败 | 降级 → 规则 / clarification | 不给 500 |
| 用户显式传 `agentCode` 但没权限 | 保持现有 `AI_AUTHORIZATION` / 403 | 不变 |

----

## 9. 安全与配额

- Supervisor 路由阶段**只读**，不操作业务数据。
- 最终执行 Agent 的 `risk_appetite`、`daily_quota`、`allowed_tools` 完全生效。
- Supervisor 只看用户有权限且已启用的 Agent，不会路由到无权限领域。
- Supervisor 的 LLM 调用计入同一次对话的 `usage_limits.request_limit`，防止无限路由循环。
- 路由调用不单独消耗 `ai_quota_rejected_total` 配额度量，只占用请求次数上限。

----

## 10. 前端改动

- `chat-input.vue` 的 Agent 下拉框新增 **“自动”** 选项。
- 发送消息时，`agentCode` 传当前选择值；选“自动”时传 `"auto"`。
- assistant 消息头部显示当前处理 Agent 名称。
- 处理 `clarification_required` 事件，渲染候选 Agent 卡片。

----

## 11. 测试策略

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/modules/ai/agents/supervisor/test_router.py` | 规则唯一命中、多命中 fallback、无命中 fallback、LLM 成功/失败、clarification 阈值、权限过滤、禁用 Agent 过滤 |
| `tests/modules/ai/test_chat_supervisor.py` | 显式 `agentCode` 行为不变、`auto` 命中关键词返回对应 Agent tool 调用、模糊 query 返回 clarification 事件、无可用 Agent 返回错误 |
| 迁移测试 | `routing_keywords` 默认 `[]`，旧数据不报错 |
| 手动测试 | 前端“自动”选项、Agent 标签、clarification 卡片 |

----

## 12. 实现边界

**In scope**：
- 单次路由 + “自动”模式
- 规则 + LLM 混合路由
- `routing_keywords` DB 列 + seed 默认值
- `clarification_required` SSE 事件
- 前端 Agent 选择器加“自动”及 Agent 显示
- 当前用户可用 Agent 过滤

**Out of scope**：
- 一轮内多 Agent 协作
- 会话中途自动切换 Agent
- Agent marketplace / 插件机制
- 自定义 Supervisor 模型
- 基于历史上下文的复杂路由

----

## 13. 决策记录

1. **Supervisor 只做单次路由** — 降低实现复杂度和审计难度，v1.5+ 先不实现多 Agent 协作。
2. **路由关键词存 `ai_agent.routing_keywords`** — 与 Agent 生命周期一致，支持管理后台编辑。
3. **`agentCode` 为空等价于 `"auto"`** — 降低新用户认知门槛。
4. **LLM 分类失败降级到 clarification** — 避免 500，把最终决定权交还用户。
5. **Supervisor 不单独计用户配额** — 仅占用同一次对话的 `usage_limits.request_limit`。
6. **用户无权限 / 禁用 Agent 不参与路由** — 安全底线，防止泄露未授权业务域。

----

## 14. 参考

- `hohu-admin/docs/specs/2026-07-02-ai-tool-gateway-design.md` §5.4 / §14
- `hohu-admin/app/modules/ai/models/agent.py`
- `hohu-admin/app/modules/ai/api/chat.py`
- `hohu-admin/app/modules/ai/service/chat_service.py`
- `hohu-admin/scripts/seed_ai_agents.py`
- `hohu-admin-web/src/views/ai/chat/modules/chat-input.vue`
