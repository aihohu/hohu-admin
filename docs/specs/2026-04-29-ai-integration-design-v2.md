# AI 集成设计方案 v2（插件化架构）

> 基于 v1 方案的架构升级 | 状态：已批准 | 日期：2026-04-29

## 1. 概述

基于 v1 设计方案（`2026-04-28-ai-integration-design.md`）的架构升级。核心变化：

1. **AI 不做单一模块**，而是 AI Core 基础设施 + 多种内置 Agent
2. **Agent 和工具通过注册表管理**，Phase 1 用简单 Python 字典，后续平滑迁移到插件系统
3. **MCP 作为外部扩展标准**，任何外部系统实现 MCP Server 即可接入
4. **完整插件市场推迟到 Phase 3**，先跑通功能再提取基础设施

### 务实路径

```
阶段 0（Phase 1）           阶段 1（Phase 2）          阶段 2（Phase 3）

AI Core 内置模块    →   MCP 集成 + 插件提取   →   Agent 自动化 + 应用商店

app/modules/ai/         MCP Server/Client        ModuleDefinition
  tool_registry         @register_tool 装饰器     entry_points
  agent_registry        外部系统 MCP 接入          应用商店 UI
  内置多种 Agent                                  双轨分发
```

## 2. 技术选型

不变，仍为 **Pydantic AI >= 1.87.0**。2026 年行业定位："Best DX for type-safe Python"。

## 3. 后端模块架构

v1 方案中 `agents/` 只有一个 `chat_agent.py`，v2 改为多 Agent 注册表：

```
app/modules/ai/
├── api/
│   ├── chat.py              # 对话接口 (SSE 流式)
│   ├── conversation.py      # 会话管理 (CRUD)
│   ├── agent.py             # Agent 列表查询（新增）
│   └── provider.py          # AI 提供商配置管理
├── service/
│   ├── chat_service.py      # 对话核心逻辑
│   ├── conversation_service.py
│   └── provider_service.py  # 多提供商管理
├── models/
│   ├── provider.py          # 提供商表
│   ├── conversation.py      # 会话记录表
│   └── message.py           # 消息记录表
├── schemas/
│   ├── chat.py
│   ├── conversation.py
│   ├── provider.py
│   └── agent.py             # Agent 信息 Schema（新增）
├── agents/
│   ├── __init__.py
│   ├── base.py              # Agent 注册表 + 工厂（核心）
│   ├── general.py           # 通用助手 Agent
│   ├── customer_service.py  # AI 客服 Agent（内置）
│   ├── data_analyst.py      # AI 数据分析 Agent（内置）
│   └── tools/               # Agent 可调用的工具
│       ├── __init__.py
│       ├── system_tools.py  # 系统查询（用户统计、系统信息）
│       ├── order_tools.py   # 订单查询（客服用）
│       └── stats_tools.py   # 统计分析（数据分析用）
└── core/
    ├── config.py            # AI 配置
    └── provider_registry.py # 多提供商注册表
```

## 4. Agent 注册表（核心新增）

### 4.1 注册机制

```python
# agents/base.py
from dataclasses import dataclass, field
from typing import Callable
from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import AsyncSession

@dataclass
class AgentDefinition:
    code: str                                    # "general" / "customer_service" / "data_analyst"
    name: str                                    # "通用助手"
    description: str                             # 描述
    icon: str                                    # "mdi:robot-outline"
    system_prompt: str                           # 系统提示词
    tools: list[str] = field(default_factory=list)  # 绑定的工具名
    model_name: str | None = None                # 推荐模型（None 则用全局默认）

@dataclass
class ChatDeps:
    user_id: int
    db: AsyncSession

# 全局注册表
_agent_registry: dict[str, AgentDefinition] = {}
_tool_registry: dict[str, Callable] = {}

def register_agent(definition: AgentDefinition):
    _agent_registry[definition.code] = definition

def register_tool(name: str, func: Callable):
    _tool_registry[name] = func

def get_agent(code: str) -> AgentDefinition | None:
    return _agent_registry.get(code)

def get_all_agents() -> list[AgentDefinition]:
    return list(_agent_registry.values())

def create_agent(agent_def: AgentDefinition, model) -> Agent:
    """根据 AgentDefinition 创建 Pydantic AI Agent，自动注册绑定的工具"""
    agent = Agent(model, deps_type=ChatDeps, instructions=agent_def.system_prompt)
    for tool_name in agent_def.tools:
        tool_func = _tool_registry.get(tool_name)
        if tool_func:
            agent.tool(tool_func)
    return agent
```

### 4.2 内置 Agent

```python
# agents/general.py
register_agent(AgentDefinition(
    code="general",
    name="通用助手",
    description="hohu 管理平台 AI 助手，可查询系统数据、回答问题",
    icon="mdi:robot-outline",
    system_prompt="你是 hohu 管理平台的 AI 助手。你可以查询系统数据、回答管理相关问题。",
    tools=["get_user_stats", "get_system_info"],
))

# agents/customer_service.py
register_agent(AgentDefinition(
    code="customer_service",
    name="AI 客服",
    description="专业的客户服务助手，可查询订单、回答常见问题",
    icon="mdi:headset",
    system_prompt="你是 hohu 平台的客服助手。用专业友好的语气回答客户问题。你可以查询订单信息、搜索常见问题、查看用户资料。",
    tools=["order_lookup", "faq_search", "get_user_profile"],
))

# agents/data_analyst.py
register_agent(AgentDefinition(
    code="data_analyst",
    name="AI 数据分析",
    description="数据统计分析助手，可生成报表、分析趋势",
    icon="mdi:chart-bar",
    system_prompt="你是 hohu 平台的数据分析助手。你可以查询统计数据、分析趋势、生成报表摘要。",
    tools=["get_user_stats", "get_job_stats", "query_custom_sql"],
))
```

### 4.3 内置工具示例

```python
# agents/tools/system_tools.py
from ..base import register_tool, ChatDeps
from pydantic_ai import RunContext

@register_tool("get_user_stats")
async def get_user_stats(ctx: RunContext[ChatDeps], period: str = "today") -> str:
    """查询用户统计信息。period 可选: today, week, month"""
    stats = await query_user_stats(ctx.deps.db, period)
    return f"统计周期: {period}, 新增用户: {stats.new_count}, 活跃用户: {stats.active_count}"

@register_tool("get_system_info")
async def get_system_info(ctx: RunContext[ChatDeps]) -> str:
    """查询系统基本信息（版本、在线用户数等）"""
    ...
```

## 5. 数据模型（基于 v1 调整）

### 5.1 `ai_provider` 提供商配置表

同 v1，无变化。

### 5.2 `ai_conversation` 会话表

在 v1 基础上新增 `agent_code` 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| conversation_id | BigInteger PK (Snowflake) | 会话 ID |
| user_id | BigInteger FK → sys_user.user_id | 所属用户 |
| **agent_code** | **String(50) DEFAULT 'general'** | **Agent 类型标识（新增）** |
| title | String(200) | 会话标题 |
| model_name | String(100) | 使用的模型标识 |
| system_prompt | Text | 系统提示词 |
| status | SmallInt DEFAULT 0 | 0=活跃, 1=归档 |
| create_time | DateTime DEFAULT now() | 创建时间 |
| update_time | DateTime ON UPDATE now() | 更新时间 |

### 5.3 `ai_message` 消息表

同 v1，无变化。

### 5.4 分阶段建表策略

| 阶段 | 建表 |
|------|------|
| Phase 1 AI Core | `ai_provider` + `ai_conversation` + `ai_message` |
| Phase 2 MCP | `ai_tool`（工具定义、参数 Schema、MCP 地址、审批配置） |
| Phase 3 Agent 自动化 | `ai_agent`（从代码注册表迁移到数据库配置；指令、触发条件、调度） |

## 6. 对话 API（基于 v1 扩展）

### 6.1 新增：Agent 查询

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/ai/agent/list` | 所有已注册 Agent 列表 | 登录用户 |
| GET | `/ai/agent/{code}` | Agent 详情 | 登录用户 |

### 6.2 其余 API

会话管理、对话核心、提供商管理 同 v1，无变化。

## 7. 流式对话实现

同 v1，核心变化是 `chat` 端点根据 `conversation.agent_code` 查找 Agent 定义：

```python
# api/chat.py
@router.post("/chat")
async def chat(request: Request, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    body = await request.json()
    conversation = await conversation_service.get_by_id(db, body["conversation_id"])

    # 根据 agent_code 查找 Agent 定义 → 创建 Pydantic AI Agent
    agent_def = get_agent(conversation.agent_code)
    model = await provider_service.get_model(conversation, db)
    agent = create_agent(agent_def, model)  # 自动注册工具

    history = await chat_service.load_history(conversation.conversation_id, db)
    adapter = VercelAIAdapter(agent=agent, run_input=...)
    event_stream = adapter.run_stream(deps=ChatDeps(user.user_id, db))
    sse_stream = adapter.encode_stream(event_stream)

    return StreamingResponse(sse_stream, media_type="text/event-stream")
```

## 8. 前端设计（基于 v1 调整）

### 8.1 变化点

- 新建对话时增加 **Agent 类型选择**步骤
- 侧边栏会话列表显示 Agent 图标区分类型
- 新增 `chat-agent-select.vue` 组件

### 8.2 其余前端设计

页面结构、SSE 请求、Pinia Store、路由菜单 同 v1，仅新增 Agent 相关类型和交互。

## 9. MCP 作为外部插件（Phase 2 预告）

Phase 1 不实现 MCP，但设计上预留扩展点：

```
外部系统（ERP、CRM、自研业务系统）
    ↓ 实现 MCP Server
hohu AI Core（MCP Client，Phase 2 实现）
    ↓ 自动发现 MCP Server 的工具
    ↓ 注册到 _tool_registry
用户在聊天中使用外部系统工具
```

任何外部系统只需实现一个 MCP Server，hohu AI 就能调用其能力。这是 2026 年 AI 工具互操作的事实标准，不需要自建插件协议。

## 10. Phase 1 实施范围

### 后端交付物

1. 数据库迁移：3 张表（`ai_provider`、`ai_conversation`、`ai_message`）
2. Settings AI 配置项 + `.env.example` 更新
3. **Agent 注册表**（`agents/base.py`）：`AgentDefinition` + `register_agent` + `create_agent`
4. **内置 Agent**：通用助手、AI 客服、AI 数据分析
5. **内置工具**：系统查询、订单查询、统计分析
6. `provider_service` + `provider_registry`：多提供商注册与模型创建
7. `chat_service`：对话核心（历史加载、流式响应、消息持久化）
8. API 端点：会话 CRUD + 流式/非流式对话 + Agent 列表 + 提供商管理
9. Redis 缓存层（对话历史缓存 + 消息暂存）
10. 菜单初始化脚本

### 前端交付物

1. `src/views/ai/chat/` 聊天页面（Agent 选择 + 会话列表 + 消息流 + 输入框）
2. `src/views/ai/provider/` 提供商管理页
3. SSE 请求封装（`src/service/request/` 扩展）
4. Pinia AI Store（Agent 列表、会话状态、消息流、模型选择）
5. API 类型定义（`src/typings/api/ai.d.ts`）
6. API 服务函数（`src/service/api/ai.ts`）

### 不包含（Phase 2/3）

- MCP Server / MCP Client / MCP 工具管理
- Agent 自动化配置与调度
- 工具调用审批流
- Token 用量统计与限额
- 完整插件市场（entry_points、应用商店 UI、双轨分发）

## 11. 后续演进路径

| 阶段 | 变化 |
|------|------|
| Phase 1 → Phase 2 | Agent 注册表不变；新增 MCP Client 接入外部工具；`ai_tool` 表管理 MCP 工具 |
| Phase 2 → Phase 3 | `ai_agent` 表从代码注册表迁移到数据库配置；集成 APScheduler 实现定时 Agent；完整插件市场 |
| 外部扩展 | 任何外部系统实现 MCP Server 即可被 AI Core 发现和使用——这就是"插件"标准 |
