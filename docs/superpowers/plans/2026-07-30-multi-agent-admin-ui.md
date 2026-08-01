# Multi-Agent 管理后台 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Multi-Agent 管理后台 UI 三块功能（Agent 管理 / Role-Agent 绑定 / 路由反馈仪表盘），补齐 supervisor-routing v4 ship 后的运维 gap。

**Architecture:** 后端扩展 ai 模块的 admin API（agent / role_agent / routing_feedback），复用现有 `AuditLogMiddleware` 自动审计；前端仿 `provider` 页 + `menu-auth-modal` 模式新建 3 个页面，栈：Vue 3 + NaiveUI + UnoCSS + Pinia + @elegant-router。

**Tech Stack:** 后端 FastAPI + SQLAlchemy 2.0 async + Pydantic v2；前端 Vue 3 + NaiveUI + UnoCSS + TypeScript；测试 pytest / vitest / Playwright。

**Spec:** [`docs/superpowers/specs/2026-07-30-multi-agent-admin-ui-design.md`](../specs/2026-07-30-multi-agent-admin-ui-design.md)

**项目根：** 后端在 `hohu-admin/`，前端在 `hohu-admin-web/`，分两个 git 仓库。每个 task 的 commit 走对应仓库。

---

## Phase A: 后端（Tasks 1-12，仓库 `hohu-admin`）

### Task 1: Agent admin schemas 文件

**Files:**
- Create: `hohu-admin/app/modules/ai/schemas/agent_admin.py`

- [x] **Step 1: 写 schema 文件**

```python
"""Multi-Agent admin UI schemas (spec §6.1)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.ai.models.agent import RiskAppetite


class AgentAdminListItem(BaseModel):
    """GET /ai/admin/agents list item（不含 systemPrompt，spec 决策 #5）."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str
    code: str
    name: str
    description: str
    enabled: bool
    is_builtin: bool
    display_order: int
    model_preference: str | None = None
    daily_quota_per_user: int | None = None
    risk_appetite: RiskAppetite
    create_time: datetime
    update_time: datetime


class AgentAdminDetailItem(AgentAdminListItem):
    """GET /ai/admin/agents/{id} detail（含 systemPrompt）."""

    system_prompt: str


class AgentAdminUpdateReq(BaseModel):
    """PUT /ai/admin/agents/{id} partial update（spec §6.1）."""

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None
    enabled: bool | None = None
    display_order: int | None = Field(None, ge=0)
    system_prompt: str | None = Field(None, max_length=32 * 1024)
    model_preference: str | None = None
    daily_quota_per_user: int | None = None
    risk_appetite: RiskAppetite | None = None

    @field_validator("description")
    @classmethod
    def _validate_desc_length(cls, v: str | None) -> str | None:
        # partial update：None 表示未传，跳过校验（spec 决策 #20）
        if v is None:
            return None
        if not (50 <= len(v) <= 200):
            raise ValueError("description 长度必须在 50-200 字之间")
        return v

    @field_validator("daily_quota_per_user")
    @classmethod
    def _validate_quota(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("daily_quota_per_user 必须 ≥ 1 或 null")
        return v

    @field_validator("model_preference")
    @classmethod
    def _validate_model_pref(cls, v: str | None) -> str | None:
        if v is None:
            return None
        import re

        if not re.match(r"^[a-z0-9_-]+:[a-z0-9_-]+$", v):
            raise ValueError("model_preference 必须为 'provider:model' 格式")
        return v
```

- [x] **Step 2: lint**

```bash
cd hohu-admin && ruff check app/modules/ai/schemas/agent_admin.py && ruff format app/modules/ai/schemas/agent_admin.py
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin && git add app/modules/ai/schemas/agent_admin.py && git commit -m "feat(ai): add agent admin schemas for admin ui"
```

---

### Task 2: AgentAdminService + list endpoint + 测试

**Files:**
- Create: `hohu-admin/app/modules/ai/service/agent_admin.py`
- Modify: `hohu-admin/app/modules/ai/api/agent.py`
- Test: `hohu-admin/tests/modules/ai/test_agent_admin.py`
- Modify: `hohu-admin/app/main.py` (router prefix 已存在，确认即可)

- [x] **Step 1: 先写失败测试（list 全量返回 + 不含 systemPrompt）**

```python
"""Multi-Agent admin UI tests (spec §6.1, §9.1)."""

from datetime import datetime

import pytest
from httpx import AsyncClient

from app.db.session import AsyncSessionLocal


@pytest.fixture
async def seed_agents(db_session):
    """Fixture：插入若干 Agent 行用于测试。"""
    from app.modules.ai.models.agent import AiAgent

    a1 = AiAgent(
        agent_id=9001,
        code="shared",
        name="Shared Agent",
        description="x" * 60,
        enabled=True,
        is_builtin=True,
        display_order=0,
        system_prompt="shared prompt",
        risk_appetite="balanced",
    )
    a2 = AiAgent(
        agent_id=9002,
        code="user_mgmt",
        name="User Mgmt",
        description="y" * 60,
        enabled=True,
        is_builtin=True,
        display_order=1,
        system_prompt="um prompt",
        risk_appetite="conservative",
    )
    db_session.add_all([a1, a2])
    await db_session.flush()


async def test_list_returns_all_agents_without_query_params(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #23：GET /ai/admin/agents 无 query 参数、无分页，返回全量列表."""
    client, _ = authed_client
    resp = await client.get("/ai/admin/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    # 至少包含 seed 的 2 行
    codes = {row["code"] for row in data}
    assert {"shared", "user_mgmt"} <= codes
    # 无分页字段（不是 PageResult）
    assert isinstance(data, list)
    assert "total" not in body["data"] if isinstance(data, dict) else True


async def test_list_excludes_system_prompt(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #5：list 不返回 systemPrompt."""
    client, _ = authed_client
    resp = await client.get("/ai/admin/agents")
    data = resp.json()["data"]
    for row in data:
        assert "systemPrompt" not in row
```

- [x] **Step 2: 跑测试确认失败**

```bash
cd hohu-admin && pytest tests/modules/ai/test_agent_admin.py -v 2>&1 | head -30
```

Expected: FAIL with 404 (endpoint 不存在)

- [x] **Step 3: 写 service 骨架**

```python
"""Agent admin service (spec §6.1).

注意：Service 层不 commit，由 API 层 `await db.commit()`。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.schemas.agent_admin import (
    AgentAdminDetailItem,
    AgentAdminListItem,
    AgentAdminUpdateReq,
)


class AgentAdminService:
    async def list_agents(self, db: AsyncSession) -> list[AgentAdminListItem]:
        result = await db.execute(
            select(AiAgent).order_by(AiAgent.display_order, AiAgent.agent_id)
        )
        agents = result.scalars().all()
        return [self._to_list_item(a) for a in agents]

    async def get_agent(self, db: AsyncSession, agent_id: int) -> AgentAdminDetailItem:
        agent = await db.get(AiAgent, agent_id)
        if agent is None:
            raise NotFoundException(
                resource_type="AI Agent",
                error_code="AI_AGENT_NOT_FOUND",
            )
        return AgentAdminDetailItem(
            **self._to_list_item(agent).model_dump(),
            system_prompt=agent.system_prompt,
        )

    async def update_agent(
        self, db: AsyncSession, agent_id: int, req: AgentAdminUpdateReq
    ) -> AgentAdminDetailItem:
        agent = await db.get(AiAgent, agent_id)
        if agent is None:
            raise NotFoundException(
                resource_type="AI Agent",
                error_code="AI_AGENT_NOT_FOUND",
            )
        data = req.model_dump(exclude_unset=True)
        # 显式忽略 code / is_builtin 字段（决策 #1）
        for forbidden in ("code", "is_builtin", "agent_id"):
            data.pop(forbidden, None)
        for k, v in data.items():
            setattr(agent, k, v)
        await db.flush()
        return await self.get_agent(db, agent_id)

    @staticmethod
    def _to_list_item(a: AiAgent) -> AgentAdminListItem:
        return AgentAdminListItem(
            agent_id=str(a.agent_id),
            code=a.code,
            name=a.name,
            description=a.description,
            enabled=a.enabled,
            is_builtin=a.is_builtin,
            display_order=a.display_order,
            model_preference=a.model_preference,
            daily_quota_per_user=a.daily_quota_per_user,
            risk_appetite=a.risk_appetite,
            create_time=a.create_time,
            update_time=a.update_time,
        )


agent_admin_service = AgentAdminService()
```

- [x] **Step 4: 扩展 API 端点（在 agent.py 末尾追加 admin 路由）**

```python
# app/modules/ai/api/agent.py 末尾追加
from app.core.auth import require_permissions
from app.modules.ai.schemas.agent_admin import AgentAdminUpdateReq
from app.modules.ai.service.agent_admin import agent_admin_service


@router.get(
    "/admin",
    summary="管理端：列出所有 AI Agent（含禁用）",
    response_model=ResponseModel[list],
    dependencies=[Depends(require_permissions("ai:agent:list"))],
)
async def admin_list_agents(
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[list]:
    """决策 #2：admin 视角全量返回，与 GET /ai/agents 用户视角分离."""
    items = await agent_admin_service.list_agents(db)
    return ResponseModel.success(data=[item.model_dump(by_alias=True) for item in items])


@router.get(
    "/admin/{agent_id}",
    summary="管理端：Agent 详情",
    response_model=ResponseModel[dict],
    dependencies=[Depends(require_permissions("ai:agent:list"))],
)
async def admin_get_agent(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[dict]:
    item = await agent_admin_service.get_agent(db, agent_id)
    return ResponseModel.success(data=item.model_dump(by_alias=True))


@router.put(
    "/admin/{agent_id}",
    summary="管理端：更新 Agent 配置",
    response_model=ResponseModel[dict],
    dependencies=[Depends(require_permissions("ai:agent:edit"))],
)
async def admin_update_agent(
    agent_id: int,
    req: AgentAdminUpdateReq,
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[dict]:
    item = await agent_admin_service.update_agent(db, agent_id, req)
    await db.commit()
    return ResponseModel.success(data=item.model_dump(by_alias=True))
```

- [x] **Step 5: 跑测试，确认 list 测试通过**

```bash
cd hohu-admin && pytest tests/modules/ai/test_agent_admin.py::test_list_returns_all_agents_without_query_params tests/modules/ai/test_agent_admin.py::test_list_excludes_system_prompt -v
```

Expected: PASS

- [x] **Step 6: Commit**

```bash
cd hohu-admin && git add app/modules/ai/service/agent_admin.py app/modules/ai/api/agent.py tests/modules/ai/test_agent_admin.py && git commit -m "feat(ai): add agent admin list endpoint with tests"
```

---

### Task 3: AgentAdminService detail + update 端点测试

**Files:**
- Modify: `hohu-admin/tests/modules/ai/test_agent_admin.py`

- [x] **Step 1: 写 detail 测试（包含 not_found）**

```python
async def test_detail_returns_system_prompt(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """detail 端点返回 systemPrompt（决策 #5）。"""
    client, _ = authed_client
    resp = await client.get("/ai/admin/agents/9001")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["code"] == "shared"
    assert data["systemPrompt"] == "shared prompt"


async def test_detail_not_found(authed_client: tuple[AsyncClient, str], db_session):
    client, _ = authed_client
    resp = await client.get("/ai/admin/agents/999999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 404
    assert body.get("errorCode") == "AI_AGENT_NOT_FOUND"
```

- [x] **Step 2: 写 update happy path 测试**

```python
async def test_update_partial_skips_unsent_fields(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #20：partial update，未传字段保持原值。"""
    client, _ = authed_client
    # 仅传 enabled，不传 description
    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["enabled"] is False
    # description 保持原值（seed 的 "x" * 60）
    assert data["description"] == "x" * 60


async def test_update_code_field_ignored(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #1：code 字段被忽略，不报错。"""
    client, _ = authed_client
    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"code": "hacked_code", "name": "Renamed"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["code"] == "shared"  # 未变
    assert data["name"] == "Renamed"
```

- [x] **Step 3: 跑测试，全绿**

```bash
cd hohu-admin && pytest tests/modules/ai/test_agent_admin.py -v
```

Expected: PASS

- [x] **Step 4: Commit**

```bash
cd hohu-admin && git add tests/modules/ai/test_agent_admin.py && git commit -m "test(ai): add agent admin detail and update tests"
```

---

### Task 4: Agent update 校验测试（决策 #3, #20, #25）

**Files:**
- Modify: `hohu-admin/tests/modules/ai/test_agent_admin.py`

- [x] **Step 1: 写 description 长度边界测试**

```python
async def test_update_description_too_short(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    client, _ = authed_client
    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"description": "x" * 49},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_AGENT_DESC_LENGTH_INVALID"


async def test_update_description_too_long(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    client, _ = authed_client
    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"description": "x" * 201},
    )
    assert resp.status_code == 400


async def test_description_length_algorithm_uses_code_points(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #20：按 Python len() 计 code point，中英文同权重。"""
    client, _ = authed_client
    # 100 个中文 = 100 code points，应该通过（按字节会失败因为 UTF-8 中文=3字节）
    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"description": "中" * 100},
    )
    assert resp.status_code == 200
```

> **注意 errorCode 映射**：要让 description 长度错误返 `AI_AGENT_DESC_LENGTH_INVALID`，需在全局 exception handler 把 Pydantic ValidationError 映射到该 code。如果项目已有统一 ValidationError → 400 + `VALIDATION_ERROR` 映射，可在 service 层加显式校验抛 `BusinessRuleException(error_code="AI_AGENT_DESC_LENGTH_INVALID")`，避免改全局映射影响其他端点。**实施期先验证现有 ValidationError 映射路径，按需选择**。

- [x] **Step 2: 若需要 service 层显式校验，修改 service**

```python
# app/modules/ai/service/agent_admin.py 的 update_agent 内
async def update_agent(
    self, db: AsyncSession, agent_id: int, req: AgentAdminUpdateReq
) -> AgentAdminDetailItem:
    agent = await db.get(AiAgent, agent_id)
    if agent is None:
        raise NotFoundException(
            resource_type="AI Agent",
            error_code="AI_AGENT_NOT_FOUND",
        )
    data = req.model_dump(exclude_unset=True)
    for forbidden in ("code", "is_builtin", "agent_id"):
        data.pop(forbidden, None)

    # 显式 description 长度校验（保证 errorCode 精确）
    if "description" in data and data["description"] is not None:
        desc = data["description"]
        if not (50 <= len(desc) <= 200):
            from app.core.exceptions import BusinessRuleException

            raise BusinessRuleException(
                "description 长度必须在 50-200 字之间",
                error_code="AI_AGENT_DESC_LENGTH_INVALID",
            )

    for k, v in data.items():
        setattr(agent, k, v)
    await db.flush()
    return await self.get_agent(db, agent_id)
```

- [x] **Step 3: 写 model_preference 格式测试（决策 #25）**

```python
async def test_model_preference_format_only_no_existence_check(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #25：只校验格式，不校验 provider/model 存在性。"""
    client, _ = authed_client
    # 假 provider，格式合法
    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"modelPreference": "xxx:yyy"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["modelPreference"] == "xxx:yyy"


async def test_model_preference_invalid_format(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    client, _ = authed_client
    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"modelPreference": "invalid_no_colon"},
    )
    assert resp.status_code == 400
```

- [x] **Step 4: 跑测试，全绿**

```bash
cd hohu-admin && pytest tests/modules/ai/test_agent_admin.py -v
```

- [x] **Step 5: Commit**

```bash
cd hohu-admin && git add app/modules/ai/service/agent_admin.py tests/modules/ai/test_agent_admin.py && git commit -m "test(ai): cover agent admin validation rules"
```

---

### Task 5: Agent admin 审计 middleware 回归测试（决策 #27）

**Files:**
- Modify: `hohu-admin/tests/modules/ai/test_agent_admin.py`

- [x] **Step 1: 写审计 middleware 验证测试**

```python
async def test_put_triggers_audit_middleware(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #27：PUT 后 sys_operation_log 多一行，复用 middleware。"""
    from sqlalchemy import select

    from app.modules.system.models.operation_log import OperationLog

    client, _ = authed_client
    # 记录前 count
    before = (
        await db_session.execute(
            select(OperationLog).where(OperationLog.path.like("/ai/admin/agents/%"))
        )
    ).scalars().all()

    resp = await client.put(
        "/ai/admin/agents/9001",
        json={"enabled": False, "name": "Audit Test"},
    )
    assert resp.status_code == 200

    await db_session.expire_all()
    after = (
        await db_session.execute(
            select(OperationLog).where(OperationLog.path.like("/ai/admin/agents/%"))
        )
    ).scalars().all()

    assert len(after) == len(before) + 1
    new_log = after[-1]
    assert new_log.module == "ai"
    assert new_log.action == "update"
    assert "/ai/admin/agents/9001" in new_log.path
    # request_params 含 PUT body 全量
    params = new_log.request_params or ""
    assert "Audit Test" in params or "enabled" in params
```

> **实施前先验证**：用 `grep -n "class OperationLog" app/modules/system/models/operation_log.py` 确认字段名（`module` / `action` / `path` / `request_params`），如名字不同需调整测试。`authed_client` fixture 见 `tests/modules/ai/conftest.py` 或类似文件，确认 fixture 已存在；如不存在，参考 `tests/modules/ai/test_routing_feedback.py` 的 fixture 写法。

- [x] **Step 2: 跑测试**

```bash
cd hohu-admin && pytest tests/modules/ai/test_agent_admin.py::test_put_triggers_audit_middleware -v
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin && git add tests/modules/ai/test_agent_admin.py && git commit -m "test(ai): assert audit middleware records agent admin update"
```

---

### Task 6: RoleAgent schemas + GET endpoint + 测试

**Files:**
- Create: `hohu-admin/app/modules/ai/schemas/role_agent.py`
- Create: `hohu-admin/app/modules/ai/service/role_agent.py`
- Create: `hohu-admin/app/modules/ai/api/role_agent.py`
- Test: `hohu-admin/tests/modules/ai/test_role_agent.py`
- Modify: `hohu-admin/app/main.py` (注册新 router)

- [x] **Step 1: 写 schema**

```python
# app/modules/ai/schemas/role_agent.py
"""Role-Agent binding schemas (spec §6.3)."""

from pydantic import BaseModel, ConfigDict


class AgentRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent_id: str
    code: str
    name: str
    description: str
    enabled: bool
    is_builtin: bool
    is_shared: bool


class RoleAgentBinding(BaseModel):
    """GET /ai/role-agent/{roleId} 响应."""

    model_config = ConfigDict(populate_by_name=True)

    role_id: str
    all_agents: list[AgentRow]
    bound_agent_ids: list[str]


class RoleAgentBindReq(BaseModel):
    """PUT /ai/role-agent/{roleId} 请求."""

    model_config = ConfigDict(populate_by_name=True)

    agent_ids: list[str]
```

- [x] **Step 2: 写失败测试**

```python
# tests/modules/ai/test_role_agent.py
"""Role-Agent binding tests (spec §6.3, §9.1)."""

import pytest
from httpx import AsyncClient

from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.role import Role


@pytest.fixture
async def seed_role_agents(db_session):
    """Fixture：插 role + agents + 绑定."""
    role = Role(role_id=7001, role_code="test_role", role_name="Test", status="1")
    a1 = AiAgent(
        agent_id=9101,
        code="shared",
        name="Shared",
        description="s" * 60,
        enabled=True,
        is_builtin=True,
        display_order=0,
        risk_appetite="balanced",
    )
    a2 = AiAgent(
        agent_id=9102,
        code="user_mgmt",
        name="User",
        description="u" * 60,
        enabled=True,
        is_builtin=True,
        display_order=1,
        risk_appetite="balanced",
    )
    binding = RoleAiAgent(role_id=7001, agent_id=9102, enabled=True)
    db_session.add_all([role, a1, a2, binding])
    await db_session.flush()
    return role


async def test_get_returns_all_agents_and_bound_ids(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    client, _ = authed_client
    resp = await client.get("/ai/role-agent/7001")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["roleId"] == "7001"
    codes = {a["code"] for a in data["allAgents"]}
    assert {"shared", "user_mgmt"} <= codes
    # shared 行 isShared=True
    shared_row = next(a for a in data["allAgents"] if a["code"] == "shared")
    assert shared_row["isShared"] is True
    # 非 shared 行 isShared=False
    user_row = next(a for a in data["allAgents"] if a["code"] == "user_mgmt")
    assert user_row["isShared"] is False
    # bound_agent_ids 不含 shared
    assert "9102" in data["boundAgentIds"]
    assert "9101" not in data["boundAgentIds"]


async def test_get_excludes_soft_disabled_segment(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #19：GET 不返回 softDisabledAgentIds 段。"""
    client, _ = authed_client
    resp = await client.get("/ai/role-agent/7001")
    data = resp.json()["data"]
    # 不存在 softDisabledAgentIds 字段
    assert "softDisabledAgentIds" not in data


async def test_role_not_found_error_code_prefix(
    authed_client: tuple[AsyncClient, str], db_session
):
    """决策 #18：跨模块校验 role 不存在抛 AI_ROLE_NOT_FOUND（AI 前缀）。"""
    client, _ = authed_client
    resp = await client.get("/ai/role-agent/999999")
    assert resp.status_code == 404
    assert resp.json().get("errorCode") == "AI_ROLE_NOT_FOUND"
```

- [x] **Step 3: 写 service**

```python
# app/modules/ai/service/role_agent.py
"""Role-Agent binding service (spec §6.3).

职责：
- GET：返回 allAgents + boundAgentIds，不暴露软禁用段
- PUT：全量覆盖（DELETE+INSERT），normalize 软禁用态为 enabled=True
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
)
from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.role_agent import (
    AgentRow,
    RoleAgentBinding,
    RoleAgentBindReq,
)
from app.modules.system.models.role import Role


class RoleAgentService:
    async def get_binding(
        self, db: AsyncSession, role_id: int
    ) -> RoleAgentBinding:
        # 跨模块校验 role 存在
        role = await db.get(Role, role_id)
        if role is None:
            raise NotFoundException(
                resource_type="Role", error_code="AI_ROLE_NOT_FOUND"
            )

        # all agents
        agents = (
            (
                await db.execute(
                    select(AiAgent).order_by(AiAgent.display_order, AiAgent.agent_id)
                )
            )
            .scalars()
            .all()
        )

        # bound + enabled
        bound_rows = (
            (
                await db.execute(
                    select(RoleAiAgent.agent_id).where(
                        RoleAiAgent.role_id == role_id,
                        RoleAiAgent.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

        return RoleAgentBinding(
            role_id=str(role_id),
            all_agents=[
                AgentRow(
                    agent_id=str(a.agent_id),
                    code=a.code,
                    name=a.name,
                    description=a.description,
                    enabled=a.enabled,
                    is_builtin=a.is_builtin,
                    is_shared=(a.code == SHARED_AGENT_CODE),
                )
                for a in agents
            ],
            bound_agent_ids=[str(aid) for aid in bound_rows],
        )

    async def put_binding(
        self, db: AsyncSession, role_id: int, req: RoleAgentBindReq
    ) -> None:
        role = await db.get(Role, role_id)
        if role is None:
            raise NotFoundException(
                resource_type="Role", error_code="AI_ROLE_NOT_FOUND"
            )

        # 去重
        unique_ids = list({int(aid) for aid in req.agent_ids})

        # 校验每个 agent 存在 + 非 shared
        if unique_ids:
            rows = (
                (
                    await db.execute(
                        select(AiAgent.agent_id, AiAgent.code).where(
                            AiAgent.agent_id.in_(unique_ids)
                        )
                    )
                )
                .all()
            )
            found_ids = {r[0] for r in rows}
            missing = set(unique_ids) - found_ids
            if missing:
                raise NotFoundException(
                    resource_type="AI Agent",
                    error_code="AI_AGENT_NOT_FOUND",
                )
            # shared 拦截
            shared_hits = [r for r in rows if r[1] == SHARED_AGENT_CODE]
            if shared_hits:
                raise BusinessRuleException(
                    "shared Agent 直通所有用户，无需绑定",
                    error_code="AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN",
                )

        # 全量覆盖：DELETE + INSERT
        await db.execute(
            delete(RoleAiAgent).where(RoleAiAgent.role_id == role_id)
        )
        for aid in unique_ids:
            db.add(RoleAiAgent(role_id=role_id, agent_id=aid, enabled=True))
        await db.flush()


role_agent_service = RoleAgentService()
```

- [x] **Step 4: 写 API**

```python
# app/modules/ai/api/role_agent.py
"""Role-Agent binding endpoints (spec §6.3).

URL 走 /ai/role-agent 而非 /system/role（决策 #17）：
表归 ai 模块，service 也归 ai 模块，避免 system → ai 跨模块依赖。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.ai.schemas.role_agent import RoleAgentBindReq
from app.modules.ai.service.role_agent import role_agent_service

router = APIRouter()


@router.get(
    "/{role_id}",
    summary="获取 Role 已绑 Agent 列表 + 全量 Agent 树",
    response_model=ResponseModel[dict],
    dependencies=[Depends(require_permissions("system:role:ai-agent-auth"))],
)
async def get_role_agent_binding(
    role_id: int,
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[dict]:
    binding = await role_agent_service.get_binding(db, role_id)
    return ResponseModel.success(data=binding.model_dump(by_alias=True))


@router.put(
    "/{role_id}",
    summary="全量覆盖 Role 的 Agent 绑定",
    response_model=ResponseModel[None],
    dependencies=[Depends(require_permissions("system:role:ai-agent-auth"))],
)
async def put_role_agent_binding(
    role_id: int,
    req: RoleAgentBindReq,
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[None]:
    await role_agent_service.put_binding(db, role_id, req)
    await db.commit()
    return ResponseModel.success(data=None)
```

- [x] **Step 5: 注册 router（app/main.py 找到 ai 模块 router 注册处）**

```python
# app/main.py 内 ai 路由组注册处加：
from app.modules.ai.api.role_agent import router as ai_role_agent_router

# 加到现有 ai prefix 下
app.include_router(
    ai_role_agent_router,
    prefix="/ai/role-agent",
    tags=["ai-role-agent"],
)
```

> 实施期 Read `app/main.py` 找现有 `include_router(... prefix="/ai/..." ...)` 写法，模仿格式。

- [x] **Step 6: 跑测试，全绿**

```bash
cd hohu-admin && pytest tests/modules/ai/test_role_agent.py -v
```

- [x] **Step 7: Commit**

```bash
cd hohu-admin && git add app/modules/ai/schemas/role_agent.py app/modules/ai/service/role_agent.py app/modules/ai/api/role_agent.py app/main.py tests/modules/ai/test_role_agent.py && git commit -m "feat(ai): add role-agent binding get endpoint"
```

---

### Task 7: RoleAgent PUT 全量覆盖 + 边界测试

**Files:**
- Modify: `hohu-admin/tests/modules/ai/test_role_agent.py`

- [x] **Step 1: 写 PUT happy path 测试**

```python
async def test_put_full_replace_semantics(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #8：全量覆盖语义，未在列表里的现有绑定会被删除。"""
    client, _ = authed_client
    # 新增 9103，移除 9102
    from app.modules.ai.models.agent import AiAgent

    a3 = AiAgent(
        agent_id=9103,
        code="role_mgmt",
        name="Role",
        description="r" * 60,
        enabled=True,
        is_builtin=True,
        display_order=2,
        risk_appetite="balanced",
    )
    db_session.add(a3)
    await db_session.flush()

    resp = await client.put("/ai/role-agent/7001", json={"agentIds": ["9103"]})
    assert resp.status_code == 200

    # 验证：9102 解绑，9103 已绑
    resp2 = await client.get("/ai/role-agent/7001")
    data = resp2.json()["data"]
    assert data["boundAgentIds"] == ["9103"]


async def test_put_normalizes_soft_disabled_to_enabled(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #19：PUT normalize 软禁用行 → enabled=True。"""
    from sqlalchemy import select

    from app.modules.ai.models.role_ai_agent import RoleAiAgent

    client, _ = authed_client
    # 先把 9102 改成 enabled=False（模拟 SQL 直改软禁用）
    await db_session.execute(
        RoleAiAgent.__table__.update()
        .where(RoleAiAgent.role_id == 7001, RoleAiAgent.agent_id == 9102)
        .values(enabled=False)
    )
    await db_session.flush()

    # PUT 包含 9102 → 应覆盖为 enabled=True
    resp = await client.put("/ai/role-agent/7001", json={"agentIds": ["9102"]})
    assert resp.status_code == 200

    rows = (
        (
            await db_session.execute(
                select(RoleAiAgent.enabled).where(
                    RoleAiAgent.role_id == 7001, RoleAiAgent.agent_id == 9102
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == [True]


async def test_put_shared_binding_rejected(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #9：禁止绑定 shared Agent。"""
    client, _ = authed_client
    resp = await client.put("/ai/role-agent/7001", json={"agentIds": ["9101"]})
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN"


async def test_put_empty_array_unbinds_all(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策（§6.3）：空数组 = 解绑全部。"""
    client, _ = authed_client
    resp = await client.put("/ai/role-agent/7001", json={"agentIds": []})
    assert resp.status_code == 200

    resp2 = await client.get("/ai/role-agent/7001")
    assert resp2.json()["data"]["boundAgentIds"] == []


async def test_put_triggers_audit_middleware(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #10 + #27：PUT 后 sys_operation_log 多一行。"""
    from sqlalchemy import select

    from app.modules.system.models.operation_log import OperationLog

    client, _ = authed_client
    resp = await client.put("/ai/role-agent/7001", json={"agentIds": ["9102"]})
    assert resp.status_code == 200

    await db_session.expire_all()
    logs = (
        (
            await db_session.execute(
                select(OperationLog).where(OperationLog.path == "/ai/role-agent/7001")
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) >= 1
    last = logs[-1]
    assert last.module == "ai"
    assert last.action == "update"
    assert "9102" in (last.request_params or "")
```

- [x] **Step 2: 跑测试**

```bash
cd hohu-admin && pytest tests/modules/ai/test_role_agent.py -v
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin && git add tests/modules/ai/test_role_agent.py && git commit -m "test(ai): cover role-agent put replace and shared rejection"
```

---

### Task 8: RoutingFeedback 查询 schemas 扩展

**Files:**
- Modify: `hohu-admin/app/modules/ai/schemas/routing_feedback.py`

- [x] **Step 1: 在文件末尾追加 query schemas**

```python
# app/modules/ai/schemas/routing_feedback.py 末尾追加
from datetime import datetime

from pydantic import Field


class FeedbackListQuery(BaseModel):
    """GET /ai/routing-feedback/list 查询参数."""

    model_config = ConfigDict(populate_by_name=True)

    days: int = Field(7, ge=1, le=365)
    current: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    feedback: str = Field("wrong", pattern="^(wrong|all|correct)$")
    original_agent: str | None = Field(None, alias="originalAgent")
    corrected_agent: str | None = Field(None, alias="correctedAgent")


class TopCorrected(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: str
    name: str
    count: int


class TopWrongAgent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent_code: str
    agent_name: str
    wrong_count: int
    top_corrected: TopCorrected | None = None


class FeedbackSummary(BaseModel):
    """GET /ai/routing-feedback/summary 响应."""

    model_config = ConfigDict(populate_by_name=True)

    days: int
    total: int
    correct: int
    wrong: int
    wrong_rate: float
    top_wrong_agents: list[TopWrongAgent]


class FeedbackListItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    feedback_id: str
    message_id: str
    user_id: str
    user_name: str
    original_agent: str
    original_agent_name: str
    feedback: str
    corrected_agent: str | None = None
    corrected_agent_name: str | None = None
    trace_id: str | None = None
    create_time: datetime
```

- [x] **Step 2: lint**

```bash
cd hohu-admin && ruff check app/modules/ai/schemas/routing_feedback.py && ruff format app/modules/ai/schemas/routing_feedback.py
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin && git add app/modules/ai/schemas/routing_feedback.py && git commit -m "feat(ai): add routing feedback query schemas"
```

---

### Task 9: RoutingFeedbackQueryService + summary endpoint

**Files:**
- Create: `hohu-admin/app/modules/ai/service/routing_feedback_query.py`
- Modify: `hohu-admin/app/modules/ai/api/routing_feedback.py`
- Test: `hohu-admin/tests/modules/ai/test_routing_feedback_query.py`

- [x] **Step 1: 写 summary 失败测试**

```python
# tests/modules/ai/test_routing_feedback_query.py
"""Routing feedback query tests (spec §6.2, §9.1)."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.system.models.user import User


@pytest.fixture
async def seed_feedback(db_session):
    """插 5 行：3 wrong（其中 2 row corrected=user_mgmt）+ 2 correct."""
    agents = [
        AiAgent(
            agent_id=9201,
            code="shared",
            name="Shared",
            description="s" * 60,
            enabled=True,
            is_builtin=True,
            display_order=0,
            risk_appetite="balanced",
        ),
        AiAgent(
            agent_id=9202,
            code="user_mgmt",
            name="User Mgmt",
            description="u" * 60,
            enabled=True,
            is_builtin=True,
            display_order=1,
            risk_appetite="balanced",
        ),
        AiAgent(
            agent_id=9203,
            code="role_mgmt",
            name="Role Mgmt",
            description="r" * 60,
            enabled=True,
            is_builtin=True,
            display_order=2,
            risk_appetite="balanced",
        ),
    ]
    user = User(
        user_id=5001,
        username="fb_user",
        password="x",
        status="1",
    )
    db_session.add_all(agents + [user])

    now = datetime.now()
    feedback_rows = [
        AiRoutingFeedback(
            feedback_id=8001,
            message_id=1001,
            user_id=5001,
            original_agent="role_mgmt",
            feedback="wrong",
            corrected_agent="user_mgmt",
            create_time=now - timedelta(days=1),
        ),
        AiRoutingFeedback(
            feedback_id=8002,
            message_id=1002,
            user_id=5001,
            original_agent="role_mgmt",
            feedback="wrong",
            corrected_agent="user_mgmt",
            create_time=now - timedelta(days=2),
        ),
        AiRoutingFeedback(
            feedback_id=8003,
            message_id=1003,
            user_id=5001,
            original_agent="config_mgmt",
            feedback="wrong",
            corrected_agent="shared",
            create_time=now - timedelta(days=3),
        ),
        AiRoutingFeedback(
            feedback_id=8004,
            message_id=1004,
            user_id=5001,
            original_agent="user_mgmt",
            feedback="correct",
            corrected_agent=None,
            create_time=now - timedelta(days=1),
        ),
        AiRoutingFeedback(
            feedback_id=8005,
            message_id=1005,
            user_id=5001,
            original_agent="shared",
            feedback="correct",
            corrected_agent=None,
            create_time=now - timedelta(days=1),
        ),
    ]
    db_session.add_all(feedback_rows)
    await db_session.flush()


async def test_summary_7_day_window(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/summary?days=7")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["days"] == 7
    assert data["total"] == 5
    assert data["wrong"] == 3
    assert data["correct"] == 2
    # wrongRate = 3/5 = 0.6，保留 4 位
    assert round(data["wrongRate"], 4) == 0.6


async def test_summary_zero_division(
    authed_client: tuple[AsyncClient, str], db_session
):
    """决策（§6.2）：total=0 时 wrongRate=0（不除零）。"""
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/summary?days=7")
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["wrongRate"] == 0


async def test_summary_top_wrong_agents_sorted(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """topWrongAgents 按 wrong 数降序：role_mgmt(2) > config_mgmt(1)。"""
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/summary?days=7")
    data = resp.json()["data"]
    top = data["topWrongAgents"]
    assert len(top) >= 2
    assert top[0]["agentCode"] == "role_mgmt"
    assert top[0]["wrongCount"] == 2
    # topCorrected 众数：user_mgmt(2)
    assert top[0]["topCorrected"]["code"] == "user_mgmt"
    assert top[0]["topCorrected"]["count"] == 2


async def test_top_corrected_tie_breaker(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #21：topCorrected 并列时按 corrected_agent code ASC 取首。"""
    # 在 seed_feedback 基础上再加一行让 role_mgmt 的 corrected_agent 出现并列
    # config_mgmt 出现 1 次 user_mgmt + 1 次 shared → 并列
    # 此场景在 seed_fixture 里 role_mgmt 是 2 个 user_mgmt（不并列）
    # 这里另加 1 个 config_mgmt 改 role_mgmt，制造 config_mgmt 的 1-1 并列
    db_session.add(
        AiRoutingFeedback(
            feedback_id=8006,
            message_id=1006,
            user_id=5001,
            original_agent="config_mgmt",
            feedback="wrong",
            corrected_agent="role_mgmt",
            create_time=datetime.now() - timedelta(days=1),
        )
    )
    await db_session.flush()

    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/summary?days=7")
    data = resp.json()["data"]
    config_row = next(
        r for r in data["topWrongAgents"] if r["agentCode"] == "config_mgmt"
    )
    # config_mgmt: 1 row shared + 1 row role_mgmt → 并列 → 取 code ASC = role_mgmt
    assert config_row["topCorrected"]["code"] == "role_mgmt"
```

- [x] **Step 2: 跑测试确认失败**

```bash
cd hohu-admin && pytest tests/modules/ai/test_routing_feedback_query.py -v 2>&1 | head -20
```

- [x] **Step 3: 写 service**

```python
# app/modules/ai/service/routing_feedback_query.py
"""Routing feedback aggregate query service (spec §6.2, 决策 #22).

与 routing_feedback_service.py（POST submit）分离：submit 是 append-only 写，
本 service 是复杂聚合查询，职责正交。
"""

from datetime import datetime, timedelta

from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.ai.schemas.routing_feedback import (
    FeedbackListItem,
    FeedbackSummary,
    TopCorrected,
    TopWrongAgent,
)
from app.modules.system.models.user import User


class RoutingFeedbackQueryService:
    async def summary(self, db: AsyncSession, days: int) -> FeedbackSummary:
        cutoff = datetime.now() - timedelta(days=days)

        base = select(AiRoutingFeedback).where(
            AiRoutingFeedback.create_time >= cutoff
        )

        # total / correct / wrong
        rows = (await db.execute(base)).scalars().all()
        total = len(rows)
        correct = sum(1 for r in rows if r.feedback == "correct")
        wrong = sum(1 for r in rows if r.feedback == "wrong")
        wrong_rate = round(wrong / total, 4) if total else 0.0

        # topWrongAgents：按 original_agent 聚合 wrong 数
        wrong_rows = [r for r in rows if r.feedback == "wrong"]
        wrong_by_agent: dict[str, list[AiRoutingFeedback]] = {}
        for r in wrong_rows:
            wrong_by_agent.setdefault(r.original_agent, []).append(r)

        # 取 name map
        all_codes = set(wrong_by_agent.keys())
        corrected_codes = {
            r.corrected_agent for r in wrong_rows if r.corrected_agent
        }
        name_map = {}
        if all_codes or corrected_codes:
            name_rows = (
                await db.execute(
                    select(AiAgent.code, AiAgent.name).where(
                        AiAgent.code.in_(all_codes | corrected_codes)
                    )
                )
            ).all()
            name_map = {r[0]: r[1] for r in name_rows}

        top_wrong: list[TopWrongAgent] = []
        for code, items in wrong_by_agent.items():
            # topCorrected 众数，并列按 corrected_agent code ASC
            corrected_count: dict[str, int] = {}
            for it in items:
                if it.corrected_agent:
                    corrected_count[it.corrected_agent] = (
                        corrected_count.get(it.corrected_agent, 0) + 1
                    )
            top_corrected = None
            if corrected_count:
                # 排序：count desc, code asc
                sorted_corrected = sorted(
                    corrected_count.items(),
                    key=lambda kv: (-kv[1], kv[0]),
                )
                top_code, top_count = sorted_corrected[0]
                top_corrected = TopCorrected(
                    code=top_code,
                    name=name_map.get(top_code, top_code),
                    count=top_count,
                )
            top_wrong.append(
                TopWrongAgent(
                    agent_code=code,
                    agent_name=name_map.get(code, code),
                    wrong_count=len(items),
                    top_corrected=top_corrected,
                )
            )

        # 按 wrong_count desc, agent_code asc 排序，top 10
        top_wrong.sort(key=lambda x: (-x.wrong_count, x.agent_code))
        top_wrong = top_wrong[:10]

        return FeedbackSummary(
            days=days,
            total=total,
            correct=correct,
            wrong=wrong,
            wrong_rate=wrong_rate,
            top_wrong_agents=top_wrong,
        )

    async def list_items(
        self,
        db: AsyncSession,
        *,
        days: int,
        current: int,
        size: int,
        feedback: str,
        original_agent: str | None,
        corrected_agent: str | None,
    ) -> tuple[list[FeedbackListItem], int]:
        cutoff = datetime.now() - timedelta(days=days)

        conditions = [AiRoutingFeedback.create_time >= cutoff]
        if feedback != "all":
            conditions.append(AiRoutingFeedback.feedback == feedback)
        if original_agent:
            conditions.append(AiRoutingFeedback.original_agent == original_agent)
        if corrected_agent:
            conditions.append(AiRoutingFeedback.corrected_agent == corrected_agent)

        # join sys_user 取 username
        stmt = (
            select(AiRoutingFeedback, User.username)
            .outerjoin(User, User.user_id == AiRoutingFeedback.user_id)
            .where(*conditions)
            .order_by(desc(AiRoutingFeedback.feedback_id))
            .offset((current - 1) * size)
            .limit(size)
        )
        rows = (await db.execute(stmt)).all()

        # 取所有 agent code（original + corrected）一次性查 name
        codes = set()
        for r, _ in rows:
            codes.add(r.original_agent)
            if r.corrected_agent:
                codes.add(r.corrected_agent)
        name_map = {}
        if codes:
            name_rows = (
                await db.execute(
                    select(AiAgent.code, AiAgent.name).where(AiAgent.code.in_(codes))
                )
            ).all()
            name_map = {r[0]: r[1] for r in name_rows}

        items = [
            FeedbackListItem(
                feedback_id=str(r.feedback_id),
                message_id=str(r.message_id),
                user_id=str(r.user_id),
                user_name=username or "",
                original_agent=r.original_agent,
                original_agent_name=name_map.get(r.original_agent, r.original_agent),
                feedback=r.feedback,
                corrected_agent=r.corrected_agent,
                corrected_agent_name=(
                    name_map.get(r.corrected_agent, r.corrected_agent)
                    if r.corrected_agent
                    else None
                ),
                trace_id=r.trace_id,
                create_time=r.create_time,
            )
            for r, username in rows
        ]

        # total count
        count_stmt = select(func.count()).select_from(AiRoutingFeedback).where(*conditions)
        total = (await db.execute(count_stmt)).scalar() or 0
        return items, total


routing_feedback_query_service = RoutingFeedbackQueryService()
```

- [x] **Step 4: 扩展 API（routing_feedback.py 追加 summary + list）**

```python
# app/modules/ai/api/routing_feedback.py 末尾追加
from app.core.auth import require_permissions
from app.modules.ai.schemas.routing_feedback import FeedbackListQuery
from app.modules.ai.service.routing_feedback_query import (
    routing_feedback_query_service,
)


@router.get(
    "/summary",
    summary="路由反馈 KPI + Agent 排行",
    response_model=ResponseModel[dict],
    dependencies=[Depends(require_permissions("ai:routing-feedback:list"))],
)
async def get_routing_feedback_summary(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[dict]:
    summary = await routing_feedback_query_service.summary(db, days=days)
    return ResponseModel.success(data=summary.model_dump(by_alias=True))


@router.get(
    "/list",
    summary="路由反馈明细分页",
    response_model=ResponseModel[dict],
    dependencies=[Depends(require_permissions("ai:routing-feedback:list"))],
)
async def get_routing_feedback_list(
    query: FeedbackListQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[dict]:
    items, total = await routing_feedback_query_service.list_items(
        db,
        days=query.days,
        current=query.current,
        size=query.size,
        feedback=query.feedback,
        original_agent=query.original_agent,
        corrected_agent=query.corrected_agent,
    )
    return ResponseModel.success(
        data={
            "records": [it.model_dump(by_alias=True) for it in items],
            "total": total,
            "current": query.current,
            "size": query.size,
        }
    )
```

- [x] **Step 5: 跑 summary 测试**

```bash
cd hohu-admin && pytest tests/modules/ai/test_routing_feedback_query.py::test_summary_7_day_window tests/modules/ai/test_routing_feedback_query.py::test_summary_zero_division tests/modules/ai/test_routing_feedback_query.py::test_summary_top_wrong_agents_sorted tests/modules/ai/test_routing_feedback_query.py::test_top_corrected_tie_breaker -v
```

- [x] **Step 6: Commit**

```bash
cd hohu-admin && git add app/modules/ai/service/routing_feedback_query.py app/modules/ai/api/routing_feedback.py tests/modules/ai/test_routing_feedback_query.py && git commit -m "feat(ai): add routing feedback summary endpoint"
```

---

### Task 10: Routing feedback list endpoint 测试

**Files:**
- Modify: `hohu-admin/tests/modules/ai/test_routing_feedback_query.py`

- [x] **Step 1: 写 list 测试**

```python
async def test_list_default_filter_wrong_only(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #6：list 默认 feedback=wrong。"""
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/list?days=7")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3
    for row in data["records"]:
        assert row["feedback"] == "wrong"


async def test_list_supports_feedback_all(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/list?days=7&feedback=all")
    data = resp.json()["data"]
    assert data["total"] == 5


async def test_list_supports_original_agent_filter(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    client, _ = authed_client
    resp = await client.get(
        "/ai/routing-feedback/list?days=7&originalAgent=role_mgmt"
    )
    data = resp.json()["data"]
    assert data["total"] == 2
    for row in data["records"]:
        assert row["originalAgent"] == "role_mgmt"


async def test_list_no_message_content_leak(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #7：list 不返回 message content。"""
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/list?days=7")
    data = resp.json()["data"]
    for row in data["records"]:
        assert "content" not in row
        assert "messageContent" not in row


async def test_list_explicit_order_by_feedback_id(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #15：list 按 feedback_id DESC 显式排序（不依赖 create_time DESC）。"""
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/list?days=7&size=10")
    data = resp.json()["data"]
    ids = [int(r["feedbackId"]) for r in data["records"]]
    assert ids == sorted(ids, reverse=True)


async def test_list_joins_username(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/list?days=7")
    data = resp.json()["data"]
    for row in data["records"]:
        if row["userId"] == "5001":
            assert row["userName"] == "fb_user"


async def test_list_user_deleted_returns_empty_username(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """LEFT JOIN：用户被删时 userName=''。"""
    from sqlalchemy import delete as sql_delete

    from app.modules.system.models.user import User

    await db_session.execute(sql_delete(User).where(User.user_id == 5001))
    await db_session.flush()

    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/list?days=7")
    data = resp.json()["data"]
    for row in data["records"]:
        assert row["userName"] == ""
```

- [x] **Step 2: 跑测试**

```bash
cd hohu-admin && pytest tests/modules/ai/test_routing_feedback_query.py -v
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin && git add tests/modules/ai/test_routing_feedback_query.py && git commit -m "test(ai): cover routing feedback list filters and ordering"
```

---

### Task 11: Agent admin lint + 全量测试 + 覆盖率

**Files:** 无新文件

- [x] **Step 1: 全量 lint**

```bash
cd hohu-admin && ruff check . && ruff format .
```

- [x] **Step 2: 全量 pytest**

```bash
cd hohu-admin && pytest tests/modules/ai/ -v
```

Expected: 全绿

- [x] **Step 3: 覆盖率检查**

```bash
cd hohu-admin && pytest tests/modules/ai/test_agent_admin.py tests/modules/ai/test_role_agent.py tests/modules/ai/test_routing_feedback_query.py --cov=app/modules/ai --cov-report=term-missing
```

Expected: 三个新 service + api 文件覆盖率 ≥ 70%

- [x] **Step 4: Commit（如 lint 修了格式）**

```bash
cd hohu-admin && git add -p && git commit -m "style(ai): apply ruff format to admin module"
```

---

### Task 12: 菜单 + 权限码 seed

**Files:**
- Modify: `hohu-admin/scripts/sync_menus.py`

- [x] **Step 1: 翻转 ai_agent 菜单 hide_in_menu（行 ~1184）**

```python
# scripts/sync_menus.py 内 ai_agent 菜单定义
{
    "route_name": "ai_agent",
    ...
    "hide_in_menu": False,  # 原 True，本期实现 src/views/ai/agent/index.vue 后改回 False
    ...
}
```

> 注意：`sync_menus.py` 是增量同步（只 add 不 update），改 `hide_in_menu` 不会自动迁移已 seed 的行。**需要写一次性 SQL 升级脚本或在 sync_menus 内加 update 逻辑**。最简方案：在 `sync_menus()` 函数末尾加 update 兜底。

- [x] **Step 2: 在 sync_menus.py 末尾加 ai_routing_feedback 菜单（在 ai_agent 之后）**

```python
# scripts/sync_menus.py MENU_DEFINITIONS 末尾追加
{
    "route_name": "ai_routing_feedback",
    "parent_route": "ai",
        "menu_name": "路由反馈分析",
        "menu_type": "C",
        "icon": "carbon:analytics",
        "icon_type": "1",
        "component": "view.ai_routing_feedback",
        "page": "ai_routing_feedback",
        "route_path": "/ai/routing-feedback",
        "i18n_key": "route.ai_routing_feedback",
        "order": 4,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
},
# ---- 路由反馈分析按钮权限 ----
{
    "key": "ai_routing_feedback_list",
    "parent_route": "ai_routing_feedback",
    "menu_name": "查询",
    "menu_type": "F",
    "permission": "ai:routing-feedback:list",
    "route_path": "",
    "status": "1",
},
# ---- Role-Agent 授权按钮（挂在 system/role 下，决策 #4 命名族） ----
{
    "key": "system_role_ai_agent_auth",
    "parent_route": "role",
    "menu_name": "AI Agent 授权",
    "menu_type": "F",
    "permission": "system:role:ai-agent-auth",
    "route_path": "",
    "status": "1",
},
```

- [x] **Step 3: 在 sync_menus() 末尾加 hide_in_menu 兜底 update**

```python
# scripts/sync_menus.py sync_menus() 函数末尾追加
# 一次性兜底：把 ai_agent 菜单的 hide_in_menu 从 True 改 False
# （增量 sync 默认不 update 已有行，需要显式 update）
result = await db.execute(
    select(Menu).where(Menu.route_name == "ai_agent")
)
ai_agent_menu = result.scalars().first()
if ai_agent_menu and ai_agent_menu.hide_in_menu:
    ai_agent_menu.hide_in_menu = False
    await db.commit()
    print(f"Updated ai_agent menu: hide_in_menu -> False")
```

- [x] **Step 4: 跑 sync_menus 验证**

```bash
cd hohu-admin && python scripts/sync_menus.py
```

Expected: 输出 "Inserted ..." 或 "Updated ai_agent menu: hide_in_menu -> False"

- [x] **Step 5: Commit**

```bash
cd hohu-admin && git add scripts/sync_menus.py && git commit -m "feat(ai): seed ai routing feedback menu and role-agent auth permission"
```

---

## Phase B: 前端（Tasks 13-21，仓库 `hohu-admin-web`）

### Task 13: 前端 types

**Files:**
- Create: `hohu-admin-web/src/typings/api/ai-agent.ts`
- Create: `hohu-admin-web/src/typings/api/ai-routing-feedback.ts`

- [x] **Step 1: 写 ai-agent.ts 类型**

```typescript
// src/typings/api/ai-agent.ts
declare namespace Api {
  namespace AiAgent {
    type RiskAppetite = 'conservative' | 'balanced' | 'aggressive';

    interface AdminListItem {
      agentId: string;
      code: string;
      name: string;
      description: string;
      enabled: boolean;
      isBuiltin: boolean;
      displayOrder: number;
      modelPreference: string | null;
      dailyQuotaPerUser: number | null;
      riskAppetite: RiskAppetite;
      createTime: string;
      updateTime: string;
    }

    interface AdminDetailItem extends AdminListItem {
      systemPrompt: string;
    }

    interface AdminUpdateReq {
      name?: string;
      description?: string;
      enabled?: boolean;
      displayOrder?: number;
      systemPrompt?: string;
      modelPreference?: string | null;
      dailyQuotaPerUser?: number | null;
      riskAppetite?: RiskAppetite;
    }

    interface AgentRow {
      agentId: string;
      code: string;
      name: string;
      description: string;
      enabled: boolean;
      isBuiltin: boolean;
      isShared: boolean;
    }

    interface RoleAgentBinding {
      roleId: string;
      allAgents: AgentRow[];
      boundAgentIds: string[];
    }

    interface RoleAgentBindReq {
      agentIds: string[];
    }
  }
}
```

- [x] **Step 2: 写 ai-routing-feedback.ts 类型**

```typescript
// src/typings/api/ai-routing-feedback.ts
declare namespace Api {
  namespace AiRoutingFeedback {
    interface TopCorrected {
      code: string;
      name: string;
      count: number;
    }

    interface TopWrongAgent {
      agentCode: string;
      agentName: string;
      wrongCount: number;
      topCorrected: TopCorrected | null;
    }

    interface Summary {
      days: number;
      total: number;
      correct: number;
      wrong: number;
      wrongRate: number;
      topWrongAgents: TopWrongAgent[];
    }

    interface ListItem {
      feedbackId: string;
      messageId: string;
      userId: string;
      userName: string;
      originalAgent: string;
      originalAgentName: string;
      feedback: 'correct' | 'wrong';
      correctedAgent: string | null;
      correctedAgentName: string | null;
      traceId: string | null;
      createTime: string;
    }

    interface ListQuery {
      days: number;
      current: number;
      size: number;
      feedback?: 'wrong' | 'all' | 'correct';
      originalAgent?: string;
      correctedAgent?: string;
    }
  }
}
```

- [x] **Step 3: typecheck**

```bash
cd hohu-admin-web && pnpm typecheck
```

- [x] **Step 4: Commit**

```bash
cd hohu-admin-web && git add src/typings/api/ai-agent.ts src/typings/api/ai-routing-feedback.ts && git commit -m "feat(ai): add admin ui types"
```

---

### Task 14: 前端 API 封装

**Files:**
- Create: `hohu-admin-web/src/service/api/ai-agent.ts`
- Create: `hohu-admin-web/src/service/api/ai-routing-feedback.ts`
- Modify: `hohu-admin-web/src/service/api/index.ts` (export)

- [x] **Step 1: 写 ai-agent.ts API**

```typescript
// src/service/api/ai-agent.ts
import { request } from '@/service/request';

/** 管理端：列出所有 Agent */
export function fetchAgentAdminList() {
  return request<Api.AiAgent.AdminListItem[]>({
    url: '/ai/admin/agents',
    method: 'get'
  });
}

/** 管理端：Agent 详情 */
export function fetchAgentAdminDetail(agentId: string) {
  return request<Api.AiAgent.AdminDetailItem>({
    url: `/ai/admin/agents/${agentId}`,
    method: 'get'
  });
}

/** 管理端：更新 Agent */
export function fetchUpdateAgentAdmin(agentId: string, data: Api.AiAgent.AdminUpdateReq) {
  return request<boolean>({
    url: `/ai/admin/agents/${agentId}`,
    method: 'put',
    data
  });
}

/** Role-Agent 绑定：GET */
export function fetchRoleAgentBinding(roleId: string) {
  return request<Api.AiAgent.RoleAgentBinding>({
    url: `/ai/role-agent/${roleId}`,
    method: 'get'
  });
}

/** Role-Agent 绑定：PUT 全量覆盖 */
export function fetchUpdateRoleAgentBinding(roleId: string, agentIds: string[]) {
  return request<boolean>({
    url: `/ai/role-agent/${roleId}`,
    method: 'put',
    data: { agentIds }
  });
}
```

> **`Api.AiAgent` namespace 来自 Task 13**。`fetchProviderModels` 是现有端点封装（参考 `src/service/api/ai-provider.ts` 或同类文件名，实施期 grep 确认）。

- [x] **Step 2: 写 ai-routing-feedback.ts API**

```typescript
// src/service/api/ai-routing-feedback.ts
import { request } from '@/service/request';

export function fetchRoutingFeedbackSummary(days: number) {
  return request<Api.AiRoutingFeedback.Summary>({
    url: '/ai/routing-feedback/summary',
    method: 'get',
    params: { days }
  });
}

export function fetchRoutingFeedbackList(params: Api.AiRoutingFeedback.ListQuery) {
  return request<{
    records: Api.AiRoutingFeedback.ListItem[];
    total: number;
    current: number;
    size: number;
  }>({
    url: '/ai/routing-feedback/list',
    method: 'get',
    params
  });
}
```

- [x] **Step 3: 在 src/service/api/index.ts 加 export**

```typescript
// src/service/api/index.ts 末尾追加
export * from './ai-agent';
export * from './ai-routing-feedback';
```

- [x] **Step 4: typecheck + lint**

```bash
cd hohu-admin-web && pnpm typecheck && pnpm lint
```

- [x] **Step 5: Commit**

```bash
cd hohu-admin-web && git add src/service/api/ai-agent.ts src/service/api/ai-routing-feedback.ts src/service/api/index.ts && git commit -m "feat(ai): add admin api wrappers"
```

---

### Task 15: 前端 Agent 管理页

**Files:**
- Create: `hohu-admin-web/src/views/ai/agent/index.vue`
- Create: `hohu-admin-web/src/views/ai/agent/modules/agent-operate-drawer.vue`

- [x] **Step 1: 写 index.vue（参考 views/ai/provider/index.vue 模式）**

```vue
<!-- src/views/ai/agent/index.vue -->
<script setup lang="ts">
import { computed, h, onMounted, ref, shallowRef } from 'vue';
import { NButton, NTag, NSwitch, NTooltip } from 'naive-ui';
import { fetchAgentAdminList } from '@/service/api';
import { $t } from '@/locales';
import { hasAuth } from '@/composables/auth';
import { useTableOperate } from '@/hooks/common/table';
import AgentOperateDrawer from './modules/agent-operate-drawer.vue';

defineOptions({
  name: 'AiAgent',
  meta: {
    title: 'AI Agent 管理',
    i18nKey: 'route.ai_agent'
  }
});

const allAgents = shallowRef<Api.AiAgent.AdminListItem[]>([]);
const loading = shallowRef(false);

async function loadList() {
  loading.value = true;
  const { error, data } = await fetchAgentAdminList();
  if (!error) {
    allAgents.value = data;
  }
  loading.value = false;
}

const keyword = ref('');
const enabledFilter = ref<null | boolean>(null);

const filtered = computed(() => {
  return allAgents.value.filter(a => {
    if (keyword.value) {
      const kw = keyword.value.toLowerCase();
      if (
        !a.code.toLowerCase().includes(kw) &&
        !a.name.toLowerCase().includes(kw)
      ) {
        return false;
      }
    }
    if (enabledFilter.value !== null && a.enabled !== enabledFilter.value) {
      return false;
    }
    return true;
  });
});

const columns = computed(() => [
  { title: 'Code', key: 'code', width: 140 },
  { title: '名称', key: 'name', width: 140 },
  {
    title: '描述',
    key: 'description',
    render: (row: Api.AiAgent.AdminListItem) =>
      h(
        NTooltip,
        {},
        {
          trigger: () =>
            h(
              'span',
              { class: 'truncate inline-block max-w-300px align-bottom' },
              row.description
            ),
          default: () => row.description
        }
      )
  },
  {
    title: '启用',
    key: 'enabled',
    width: 80,
    render: (row: Api.AiAgent.AdminListItem) =>
      h(NSwitch, {
        value: row.enabled,
        size: 'small',
        // 只读展示，开关切换走 drawer 编辑（保证审计 + 校验）
        onUpdateValue: () => {
          // no-op
        }
      })
  },
  {
    title: '内置',
    key: 'isBuiltin',
    width: 80,
    render: (row: Api.AiAgent.AdminListItem) =>
      h(
        NTag,
        { type: row.isBuiltin ? 'info' : 'default', size: 'small' },
        { default: () => (row.isBuiltin ? '是' : '否') }
      )
  },
  { title: '排序', key: 'displayOrder', width: 80 },
  {
    title: '操作',
    key: 'actions',
    width: 100,
    fixed: 'right',
    render: (row: Api.AiAgent.AdminListItem) =>
      hasAuth('ai:agent:edit')
        ? h(
            NButton,
            {
              size: 'small',
              type: 'primary',
              text: true,
              onClick: () => openEdit(row)
            },
            { default: () => $t('common.edit') }
          )
        : null
  }
]);

const drawer = useTableOperate<Api.AiAgent.AdminListItem>(allAgents);

function openEdit(row: Api.AiAgent.AdminListItem) {
  drawer.edit(row);
}

onMounted(loadList);
</script>

<template>
  <div class="flex-col-stretch gap-4 p-4">
    <NCard>
      <NForm inline>
        <NFormItem label="关键字">
          <NInput
            v-model:value="keyword"
            placeholder="code / name"
            clearable
          />
        </NFormItem>
        <NFormItem label="启用状态">
          <NSelect
            v-model:value="enabledFilter"
            :options="[
              { label: '全部', value: null },
              { label: '启用', value: true },
              { label: '禁用', value: false }
            ]"
            class="w-120px"
          />
        </NFormItem>
      </NForm>
    </NCard>

    <NCard>
      <NDataTable
        :columns="columns"
        :data="filtered"
        :loading="loading"
      />
    </NCard>

    <AgentOperateDrawer
      v-model:visible="drawer.visible"
      :operate-type="drawer.operateType"
      :edit-row="drawer.editingData"
      :on-submit="loadList"
    />
  </div>
</template>
```

> **`hasAuth` 来自 `@/composables/auth`**（CLAUDE.md §Button-level Permission Control：TSX/render 内用 `hasAuth`，模板内用 `v-permission` directive）。实施期 `grep -rn "export.*hasAuth" hohu-admin-web/src/` 确认路径。

- [x] **Step 2: 写 agent-operate-drawer.vue**

```vue
<!-- src/views/ai/agent/modules/agent-operate-drawer.vue -->
<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import { FormInst } from 'naive-ui';
import {
  fetchAgentAdminDetail,
  fetchUpdateAgentAdmin
} from '@/service/api';
import { $t } from '@/locales';

interface Props {
  visible: boolean;
  operateType: 'add' | 'edit';
  editRow: Api.AiAgent.AdminListItem | null;
  onSubmit: () => void;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void;
}>();

const visible = computed({
  get: () => props.visible,
  set: v => emit('update:visible', v)
});

const formRef = ref<FormInst | null>(null);
const model = ref<Api.AiAgent.AdminUpdateReq & { code?: string }>({});
const detail = shallowRef<Api.AiAgent.AdminDetailItem | null>(null);
const modelPreferenceOptions = shallowRef<{ label: string; value: string }[]>([
  { label: '用全局默认', value: '' }
]);
const descLen = computed(() => model.value.description?.length ?? 0);
const descInvalid = computed(() => {
  // 仅在 description 字段被用户编辑过时才校验
  if (model.value.description === undefined) return false;
  return descLen.value < 50 || descLen.value > 200;
});

async function loadDetail() {
  if (!props.editRow) return;
  const { data } = await fetchAgentAdminDetail(props.editRow.agentId);
  detail.value = data;
  model.value = {
    name: data.name,
    description: data.description,
    enabled: data.enabled,
    displayOrder: data.displayOrder,
    systemPrompt: data.systemPrompt,
    modelPreference: data.modelPreference,
    dailyQuotaPerUser: data.dailyQuotaPerUser,
    riskAppetite: data.riskAppetite
  };
  model.value.code = data.code;
}

async function loadModelOptions() {
  // 复用现有 GET /ai/provider/models 端点（CLAUDE.md AI Module）
  const { fetchProviderModels } = await import('@/service/api');
  const { data } = await fetchProviderModels();
  // data 是 { providerId, providerCode, providerName, model, modelId }[]
  // 转 "providerCode:model" 格式
  const opts = data.map(m => ({
    label: `${m.providerName} / ${m.model}`,
    value: `${m.providerCode}:${m.model}`
  }));
  modelPreferenceOptions.value = [
    { label: '用全局默认', value: '' },
    ...opts
  ];
}

async function handleSubmit() {
  if (descInvalid.value) return;
  if (!props.editRow) return;
  // model_preference 空串转 null
  const body: Api.AiAgent.AdminUpdateReq = { ...model.value };
  if (body.modelPreference === '') body.modelPreference = null;
  delete (body as { code?: string }).code;
  const { error } = await fetchUpdateAgentAdmin(
    props.editRow.agentId,
    body
  );
  if (!error) {
    window.$message?.success?.($t('common.modifySuccess'));
    visible.value = false;
    props.onSubmit();
  }
}

watch(
  () => props.visible,
  v => {
    if (v) {
      loadDetail();
      loadModelOptions();
    }
  }
);
</script>

<template>
  <NDrawer v-model:show="visible" :width="600">
    <NDrawerContent title="编辑 Agent" closable>
      <NForm ref="formRef" :model="model" label-placement="top">
        <NFormItem label="Code">
          <NInput :value="model.code" disabled />
        </NFormItem>
        <NFormItem label="名称">
          <NInput v-model:value="model.name" />
        </NFormItem>
        <NFormItem label="启用">
          <NSwitch v-model:value="model.enabled" />
        </NFormItem>
        <NFormItem label="排序">
          <NInputNumber v-model:value="model.displayOrder" :min="0" />
        </NFormItem>
        <NFormItem label="风险偏好">
          <NSelect
            v-model:value="model.riskAppetite"
            :options="[
              { label: 'Conservative', value: 'conservative' },
              { label: 'Balanced', value: 'balanced' },
              { label: 'Aggressive', value: 'aggressive' }
            ]"
          />
        </NFormItem>
        <NFormItem label="每日配额/用户（空=仅全局 L2）">
          <NInputNumber
            v-model:value="model.dailyQuotaPerUser"
            :min="1"
            clearable
          />
        </NFormItem>
        <NFormItem label="模型偏好">
          <NSelect
            v-model:value="model.modelPreference"
            :options="modelPreferenceOptions"
          />
        </NFormItem>
        <NFormItem label="描述">
          <NInput
            v-model:value="model.description"
            type="textarea"
            :autosize="{ minRows: 3 }"
          />
          <template #feedback>
            <span :class="{ 'text-red-500': descInvalid }">
              {{ descLen }} / 50-200
            </span>
          </template>
        </NFormItem>
        <NFormItem label="System Prompt">
          <NInput
            v-model:value="model.systemPrompt"
            type="textarea"
            :autosize="{ minRows: 6 }"
          />
        </NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="visible = false">{{ $t('common.cancel') }}</NButton>
          <NButton
            type="primary"
            :disabled="descInvalid"
            @click="handleSubmit"
          >
            {{ $t('common.confirm') }}
          </NButton>
        </NSpace>
      </template>
    </NDrawerContent>
  </NDrawer>
</template>
```

- [x] **Step 3: dev server 启动验证**

```bash
cd hohu-admin-web && pnpm dev
```

浏览器打开 `http://localhost:9527/ai/agent`，登录 R_SUPER，应能看到 Agent 列表。

- [x] **Step 4: lint + typecheck**

```bash
cd hohu-admin-web && pnpm lint && pnpm typecheck
```

- [x] **Step 5: Commit**

```bash
cd hohu-admin-web && git add src/views/ai/agent/ && git commit -m "feat(ai): add agent management page with edit drawer"
```

---

### Task 16: 前端 Role-Agent modal + Role 列表按钮

**Files:**
- Create: `hohu-admin-web/src/views/system/role/modules/ai-agent-auth-modal.vue`
- Modify: `hohu-admin-web/src/views/system/role/index.vue` (加按钮)

- [x] **Step 1: 写 modal（仿 menu-auth-modal.vue）**

```vue
<!-- src/views/system/role/modules/ai-agent-auth-modal.vue -->
<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue';
import {
  fetchRoleAgentBinding,
  fetchUpdateRoleAgentBinding
} from '@/service/api';
import { $t } from '@/locales';

defineOptions({
  name: 'AiAgentAuthModal'
});

interface Props {
  roleId: string;
}
const props = defineProps<Props>();

const visible = defineModel<boolean>('visible', {
  default: false
});

function closeModal() {
  visible.value = false;
}

const title = computed(() => 'AI Agent 授权');

const showSpin = shallowRef(false);
const allAgents = shallowRef<Api.AiAgent.AgentRow[]>([]);
const checkedIds = shallowRef<string[]>([]);

async function loadBinding() {
  if (!props.roleId) return;
  showSpin.value = true;
  const { error, data } = await fetchRoleAgentBinding(props.roleId);
  if (!error) {
    allAgents.value = data.allAgents;
    checkedIds.value = [...data.boundAgentIds];
  }
  showSpin.value = false;
}

async function handleSubmit() {
  const { error } = await fetchUpdateRoleAgentBinding(props.roleId, checkedIds.value);
  if (!error) {
    window.$message?.success?.($t('common.modifySuccess'));
    closeModal();
  }
}

watch(visible, v => {
  if (v) {
    loadBinding();
  }
});
</script>

<template>
  <NModal v-model:show="visible" :title="title" preset="card" class="w-480px">
    <NSpin :show="showSpin">
      <NCheckboxGroup v-model:value="checkedIds">
        <NSpace vertical>
          <div
            v-for="agent in allAgents"
            :key="agent.agentId"
            class="flex-y-center gap-12px"
          >
            <NCheckbox
              :value="agent.agentId"
              :disabled="agent.isShared"
              :label="agent.name + ' (' + agent.code + ')'"
            />
            <NTag v-if="agent.isShared" size="small" type="info">
              shared 直通
            </NTag>
          </div>
        </NSpace>
      </NCheckboxGroup>
      <NAlert type="info" class="mt-12px" :bordered="false">
        shared Agent 直通所有用户，无需勾选。
      </NAlert>
    </NSpin>
    <template #footer>
      <NSpace justify="end">
        <NButton size="small" @click="closeModal">
          {{ $t('common.cancel') }}
        </NButton>
        <NButton type="primary" size="small" @click="handleSubmit">
          {{ $t('common.confirm') }}
        </NButton>
      </NSpace>
    </template>
  </NModal>
</template>
```

- [x] **Step 2: 在 role/index.vue 加按钮（找操作列）**

```vue
<!-- src/views/system/role/index.vue 内操作列加按钮 -->
<!-- 模板顶部 import -->
<script setup lang="ts">
import AiAgentAuthModal from './modules/ai-agent-auth-modal.vue';
import { shallowRef } from 'vue';

const aiAgentAuthVisible = shallowRef(false);
const aiAgentAuthRoleId = shallowRef('');

function openAiAgentAuth(roleId: string) {
  aiAgentAuthRoleId.value = roleId;
  aiAgentAuthVisible.value = true;
}
</script>

<!-- 操作列加按钮（用 v-permission） -->
<template>
  <!-- ... -->
  <NButton
    v-permission="'system:role:ai-agent-auth'"
    size="small"
    type="primary"
    text
    @click="openAiAgentAuth(row.roleId)"
  >
    AI Agent 授权
  </NButton>
  <!-- ... -->

  <AiAgentAuthModal
    v-model:visible="aiAgentAuthVisible"
    :role-id="aiAgentAuthRoleId"
  />
</template>
```

> 实施期 Read `src/views/system/role/index.vue` 找现有"菜单权限"按钮位置，紧邻加新按钮。注意按钮的 v-permission 字符串必须精确匹配后端种子 `'system:role:ai-agent-auth'`。

- [x] **Step 3: dev 验证**

```bash
cd hohu-admin-web && pnpm dev
```

浏览器 `/system/role` → 行内点 "AI Agent 授权" → modal 出现 → shared 行 disabled → 勾选提交。

- [x] **Step 4: lint + typecheck**

```bash
cd hohu-admin-web && pnpm lint && pnpm typecheck
```

- [x] **Step 5: Commit**

```bash
cd hohu-admin-web && git add src/views/system/role/ && git commit -m "feat(system-role): add ai agent auth modal"
```

---

### Task 17: 前端路由反馈仪表盘

**Files:**
- Create: `hohu-admin-web/src/views/ai/routing-feedback/index.vue`

- [x] **Step 1: 写 dashboard**

```vue
<!-- src/views/ai/routing-feedback/index.vue -->
<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue';
import {
  fetchRoutingFeedbackList,
  fetchRoutingFeedbackSummary
} from '@/service/api';
import { useNaivePaginatedTable } from '@/hooks/common/table';

defineOptions({
  name: 'AiRoutingFeedback',
  meta: {
    title: '路由反馈分析',
    i18nKey: 'route.ai_routing_feedback'
  }
});

const days = ref<7 | 30>(7);
const originalAgent = ref<string | null>(null);
const correctedAgent = ref<string | null>(null);

const summary = shallowRef<Api.AiRoutingFeedback.Summary | null>(null);
const summaryLoading = shallowRef(false);

async function loadSummary() {
  summaryLoading.value = true;
  const { error, data } = await fetchRoutingFeedbackSummary(days.value);
  if (!error) {
    summary.value = data;
  }
  summaryLoading.value = false;
}

const listQuery = computed(() => ({
  days: days.value,
  current: current.value,
  size: size.value,
  feedback: 'wrong' as const,
  originalAgent: originalAgent.value || undefined,
  correctedAgent: correctedAgent.value || undefined
}));

const {
  columns,
  data: listData,
  loading: listLoading,
  current,
  size,
  total,
  getData: reloadList
} = useNaivePaginatedTable<
  Api.AiRoutingFeedback.ListItem,
  { days: number; originalAgent?: string; correctedAgent?: string }
>(
  async params => {
    const { error, data } = await fetchRoutingFeedbackList({
      ...params,
      current: params.current || 1,
      size: params.size || 20,
      feedback: 'wrong'
    });
    if (!error) {
      return { records: data.records, total: data.total };
    }
    return { records: [], total: 0 };
  },
  {
    columnsFactory: () => [
      { title: '时间', key: 'createTime', width: 180 },
      { title: '用户', key: 'userName', width: 120 },
      {
        title: '原 Agent → 纠正 Agent',
        key: 'agentFlow',
        render: (row: Api.AiRoutingFeedback.ListItem) =>
          `${row.originalAgentName} → ${row.correctedAgentName || '-'}`
      },
      {
        title: 'TraceId',
        key: 'traceId',
        width: 200,
        render: (row: Api.AiRoutingFeedback.ListItem) =>
          row.traceId
            ? h(
                'a',
                {
                  class: 'text-primary underline cursor-pointer',
                  onClick: () => {
                    window.open(
                      `/monitor/operation-log?traceId=${row.traceId}`,
                      '_blank'
                    );
                  }
                },
                row.traceId.slice(0, 8) + '...'
              )
            : '-'
      }
    ]
  }
);

watch(days, () => {
  loadSummary();
  reloadList();
});

watch([originalAgent, correctedAgent], () => {
  reloadList();
});

onMounted(() => {
  loadSummary();
  reloadList();
});

import { h } from 'vue';
</script>

<template>
  <div class="flex-col-stretch gap-4 p-4">
    <NCard>
      <NRadioGroup v-model:value="days">
        <NRadio :value="7">近 7 天</NRadio>
        <NRadio :value="30">近 30 天</NRadio>
      </NRadioGroup>
    </NCard>

    <NGrid :cols="4" :x-gap="12">
      <NGi>
        <NCard>
          <NStatistic label="总反馈" :value="summary?.total ?? 0" />
        </NCard>
      </NGi>
      <NGi>
        <NCard>
          <NStatistic label="正确反馈" :value="summary?.correct ?? 0" />
        </NCard>
      </NGi>
      <NGi>
        <NCard>
          <NStatistic label="错路由" :value="summary?.wrong ?? 0" />
        </NCard>
      </NGi>
      <NGi>
        <NCard>
          <NStatistic
            label="错路由率"
            :value="((summary?.wrongRate ?? 0) * 100).toFixed(2) + '%'"
          />
        </NCard>
      </NGi>
    </NGrid>

    <NCard title="错路由 Agent 排行（top 10）">
      <NDataTable
        :columns="[
          { title: 'Agent', key: 'agentName' },
          { title: '错路由数', key: 'wrongCount' },
          {
            title: '最常被纠正到',
            key: 'topCorrected',
            render: (r: any) =>
              r.topCorrected
                ? r.topCorrected.name + ' (' + r.topCorrected.count + ')'
                : '-'
          }
        ]"
        :data="summary?.topWrongAgents ?? []"
        :loading="summaryLoading"
      />
    </NCard>

    <NCard title="明细">
      <NForm inline>
        <NFormItem label="原 Agent">
          <NInput v-model:value="originalAgent" clearable />
        </NFormItem>
        <NFormItem label="纠正 Agent">
          <NInput v-model:value="correctedAgent" clearable />
        </NFormItem>
      </NForm>
      <NDataTable
        :columns="columns"
        :data="listData"
        :loading="listLoading"
        remote
        :pagination="{
          page: current,
          pageSize: size,
          itemCount: total,
          showSizePicker: true,
          pageSizes: [10, 20, 50]
        }"
        @update:page="(p: number) => { current = p; reloadList(); }"
        @update:page-size="(s: number) => { size = s; current = 1; reloadList(); }"
      />
    </NCard>
  </div>
</template>
```

- [x] **Step 2: dev 验证**

```bash
cd hohu-admin-web && pnpm dev
```

浏览器 `/ai/routing-feedback` → 切 7/30 天 → KPI 变 → 点 traceId 新 tab 跳。

- [x] **Step 3: lint + typecheck**

```bash
cd hohu-admin-web && pnpm lint && pnpm typecheck
```

- [x] **Step 4: Commit**

```bash
cd hohu-admin-web && git add src/views/ai/routing-feedback/ && git commit -m "feat(ai): add routing feedback dashboard"
```

---

### Task 18: 前端 i18n

**Files:**
- Modify: `hohu-admin-web/src/locales/langs/zh-cn.ts`
- Modify: `hohu-admin-web/src/locales/langs/en-us.ts`

- [x] **Step 1: zh-cn.ts 加翻译**

在 `route` 命名空间加：

```typescript
ai_agent: 'AI Agent 管理',
ai_routing_feedback: '路由反馈分析',
```

在 `page.ai` 命名空间加（如果没有就新建）：

```typescript
agent: {
  title: 'AI Agent 管理',
  description: '描述',
  descriptionHint: '50-200 字（中英文统一按字符计）',
  systemPrompt: 'System Prompt',
  riskAppetite: '风险偏好',
  dailyQuotaPerUser: '每日配额/用户',
  modelPreference: '模型偏好',
  useGlobalDefault: '用全局默认'
},
routingFeedback: {
  title: '路由反馈分析',
  last7Days: '近 7 天',
  last30Days: '近 30 天',
  total: '总反馈',
  correct: '正确反馈',
  wrong: '错路由',
  wrongRate: '错路由率',
  topWrongAgents: '错路由 Agent 排行',
  detail: '明细',
  originalAgent: '原 Agent',
  correctedAgent: '纠正 Agent'
},
aiAgentAuth: {
  title: 'AI Agent 授权',
  sharedHint: 'shared Agent 直通所有用户，无需勾选'
}
```

在 `errorCode` 命名空间加：

```typescript
AI_AGENT_NOT_FOUND: 'Agent 不存在',
AI_AGENT_DESC_LENGTH_INVALID: '描述长度必须在 50-200 字之间',
AI_ROLE_NOT_FOUND: '角色不存在',
AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN: 'shared Agent 直通所有用户，无需绑定',
```

- [x] **Step 2: en-us.ts 同步加英文翻译**

```typescript
ai_agent: 'AI Agent Management',
ai_routing_feedback: 'Routing Feedback Analytics',

agent: {
  title: 'AI Agent Management',
  description: 'Description',
  descriptionHint: '50-200 chars (counted by code point)',
  systemPrompt: 'System Prompt',
  riskAppetite: 'Risk Appetite',
  dailyQuotaPerUser: 'Daily Quota/User',
  modelPreference: 'Model Preference',
  useGlobalDefault: 'Use Global Default'
},
routingFeedback: {
  title: 'Routing Feedback Analytics',
  last7Days: 'Last 7 days',
  last30Days: 'Last 30 days',
  total: 'Total Feedback',
  correct: 'Correct',
  wrong: 'Wrong',
  wrongRate: 'Wrong Rate',
  topWrongAgents: 'Top Wrong Agents',
  detail: 'Detail',
  originalAgent: 'Original Agent',
  correctedAgent: 'Corrected Agent'
},
aiAgentAuth: {
  title: 'AI Agent Authorization',
  sharedHint: 'Shared agent bypasses all users, no need to bind'
}
```

errorCode 加：

```typescript
AI_AGENT_NOT_FOUND: 'Agent not found',
AI_AGENT_DESC_LENGTH_INVALID: 'Description length must be between 50 and 200 chars',
AI_ROLE_NOT_FOUND: 'Role not found',
AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN:
  'Shared agent bypasses all users, no binding needed',
```

- [x] **Step 3: lint + typecheck**

```bash
cd hohu-admin-web && pnpm lint && pnpm typecheck
```

- [x] **Step 4: Commit**

```bash
cd hohu-admin-web && git add src/locales/langs/zh-cn.ts src/locales/langs/en-us.ts && git commit -m "feat(ai): add i18n for admin ui"
```

---

### Task 19: 前端 vitest - Agent drawer

**Files:**
- Create: `hohu-admin-web/src/views/ai/agent/__tests__/agent-operate-drawer.spec.ts`

- [x] **Step 1: 写 spec**

```typescript
// src/views/ai/agent/__tests__/agent-operate-drawer.spec.ts
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import AgentOperateDrawer from '../modules/agent-operate-drawer.vue';

vi.mock('@/service/api', () => ({
  fetchAgentAdminDetail: vi.fn().mockResolvedValue({
    error: null,
    data: {
      agentId: '1',
      code: 'user_mgmt',
      name: 'User',
      description: 'a'.repeat(60),
      enabled: true,
      isBuiltin: true,
      displayOrder: 1,
      modelPreference: null,
      dailyQuotaPerUser: null,
      riskAppetite: 'balanced',
      systemPrompt: 'sp',
      createTime: '',
      updateTime: ''
    }
  }),
  fetchUpdateAgentAdmin: vi.fn().mockResolvedValue({ error: null }),
  fetchProviderModels: vi.fn().mockResolvedValue({ data: [] })
}));

vi.mock('@/locales', () => ({
  $t: (k: string) => k
}));

describe('agent-operate-drawer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('description 字符计数实时变红（< 50 / > 200）', async () => {
    const wrapper = mount(AgentOperateDrawer, {
      props: {
        visible: true,
        operateType: 'edit',
        editRow: { agentId: '1' } as any,
        onSubmit: () => {}
      },
      global: {
        stubs: {
          NDrawer: true,
          NDrawerContent: true,
          NForm: true,
          NFormItem: true,
          NInput: true,
          NInputNumber: true,
          NSwitch: true,
          NSelect: true,
          NButton: true,
          NSpace: true
        }
      }
    });
    await flushPromises();
    // 初始 description 长度 60（合规）
    expect(wrapper.vm.descInvalid).toBe(false);
    // 改成 49 字
    (wrapper.vm as any).model.description = 'x'.repeat(49);
    await wrapper.vm.$nextTick();
    expect(wrapper.vm.descInvalid).toBe(true);
    // 改成 201 字
    (wrapper.vm as any).model.description = 'x'.repeat(201);
    await wrapper.vm.$nextTick();
    expect(wrapper.vm.descInvalid).toBe(true);
  });

  it('200 字以上保存禁用', async () => {
    const wrapper = mount(AgentOperateDrawer, {
      props: {
        visible: true,
        operateType: 'edit',
        editRow: { agentId: '1' } as any,
        onSubmit: () => {}
      },
      global: { stubs: ['NDrawer', 'NDrawerContent', 'NForm', 'NFormItem', 'NInput', 'NInputNumber', 'NSwitch', 'NSelect', 'NButton', 'NSpace'] }
    });
    await flushPromises();
    (wrapper.vm as any).model.description = 'x'.repeat(201);
    await wrapper.vm.$nextTick();
    expect(wrapper.vm.descInvalid).toBe(true);
  });

  it('提交调用 fetchUpdateAgentAdmin', async () => {
    const { fetchUpdateAgentAdmin } = await import('@/service/api');
    const wrapper = mount(AgentOperateDrawer, {
      props: {
        visible: true,
        operateType: 'edit',
        editRow: { agentId: '1' } as any,
        onSubmit: () => {}
      },
      global: { stubs: ['NDrawer', 'NDrawerContent', 'NForm', 'NFormItem', 'NInput', 'NInputNumber', 'NSwitch', 'NSelect', 'NButton', 'NSpace'] }
    });
    await flushPromises();
    await (wrapper.vm as any).handleSubmit();
    expect(fetchUpdateAgentAdmin).toHaveBeenCalled();
  });
});
```

- [x] **Step 2: 跑 vitest**

```bash
cd hohu-admin-web && pnpm vitest run src/views/ai/agent/__tests__/agent-operate-drawer.spec.ts
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin-web && git add src/views/ai/agent/__tests__/ && git commit -m "test(ai): add agent drawer vitest spec"
```

---

### Task 20: 前端 vitest - Role-Agent modal

**Files:**
- Create: `hohu-admin-web/src/views/system/role/modules/__tests__/ai-agent-auth-modal.spec.ts`

- [x] **Step 1: 写 spec**

```typescript
// src/views/system/role/modules/__tests__/ai-agent-auth-modal.spec.ts
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import AiAgentAuthModal from '../ai-agent-auth-modal.vue';

vi.mock('@/service/api', () => ({
  fetchRoleAgentBinding: vi.fn().mockResolvedValue({
    error: null,
    data: {
      roleId: '1',
      allAgents: [
        {
          agentId: '100',
          code: 'shared',
          name: 'Shared',
          description: 'shared',
          enabled: true,
          isBuiltin: true,
          isShared: true
        },
        {
          agentId: '101',
          code: 'user_mgmt',
          name: 'User Mgmt',
          description: 'u',
          enabled: true,
          isBuiltin: true,
          isShared: false
        }
      ],
      boundAgentIds: ['101']
    }
  }),
  fetchUpdateRoleAgentBinding: vi.fn().mockResolvedValue({ error: null })
}));

vi.mock('@/locales', () => ({
  $t: (k: string) => k
}));

describe('ai-agent-auth-modal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shared Agent 行 disabled', async () => {
    const wrapper = mount(AiAgentAuthModal, {
      props: { visible: true, roleId: '1' },
      global: {
        stubs: {
          NModal: true,
          NSpin: true,
          NCheckboxGroup: { template: '<div><slot/></div>' },
          NCheckbox: {
            props: ['value', 'disabled', 'label'],
            template: '<input type="checkbox" :value="value" :disabled="disabled" />'
          },
          NSpace: { template: '<div><slot/></div>' },
          NTag: true,
          NAlert: true,
          NButton: true
        }
      }
    });
    await flushPromises();
    // shared 应该 disabled
    const checkboxes = wrapper.findAll('input[type=checkbox]');
    expect(checkboxes[0].attributes('disabled')).toBeDefined();
    expect(checkboxes[1].attributes('disabled')).toBeUndefined();
  });

  it('shared 行识别走 isShared 标志，不依赖 code === "shared"', async () => {
    // 用 code !== 'shared' 但 isShared=true 的行验证
    vi.mocked(
      (await import('@/service/api')).fetchRoleAgentBinding
    ).mockResolvedValueOnce({
      error: null,
      data: {
        roleId: '1',
        allAgents: [
          {
            agentId: 'X',
            code: 'custom_shared_renamed',
            name: 'X',
            description: '',
            enabled: true,
            isBuiltin: false,
            isShared: true
          }
        ],
        boundAgentIds: []
      }
    } as any);

    const wrapper = mount(AiAgentAuthModal, {
      props: { visible: true, roleId: '1' },
      global: {
        stubs: {
          NModal: true,
          NSpin: true,
          NCheckboxGroup: { template: '<div><slot/></div>' },
          NCheckbox: {
            props: ['value', 'disabled', 'label'],
            template: '<input type="checkbox" :value="value" :disabled="disabled" />'
          },
          NSpace: { template: '<div><slot/></div>' },
          NTag: true,
          NAlert: true,
          NButton: true
        }
      }
    });
    await flushPromises();
    expect(wrapper.findAll('input[type=checkbox]')[0].attributes('disabled')).toBeDefined();
  });

  it('提交 body 排除 shared', async () => {
    const { fetchUpdateRoleAgentBinding } = await import('@/service/api');
    const wrapper = mount(AiAgentAuthModal, {
      props: { visible: true, roleId: '1' },
      global: {
        stubs: {
          NModal: true,
          NSpin: true,
          NCheckboxGroup: { template: '<div><slot/></div>' },
          NCheckbox: {
            props: ['value', 'disabled', 'label'],
            template: '<input type="checkbox" :value="value" :disabled="disabled" />'
          },
          NSpace: { template: '<div><slot/></div>' },
          NTag: true,
          NAlert: true,
          NButton: true
        }
      }
    });
    await flushPromises();
    await (wrapper.vm as any).handleSubmit();
    expect(fetchUpdateRoleAgentBinding).toHaveBeenCalledWith('1', ['101']);
  });
});
```

- [x] **Step 2: 跑 vitest**

```bash
cd hohu-admin-web && pnpm vitest run src/views/system/role/modules/__tests__/ai-agent-auth-modal.spec.ts
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin-web && git add src/views/system/role/modules/__tests__/ && git commit -m "test(system-role): add ai agent auth modal vitest spec"
```

---

### Task 21: 前端 vitest - Routing feedback dashboard

**Files:**
- Create: `hohu-admin-web/src/views/ai/routing-feedback/__tests__/index.spec.ts`

- [x] **Step 1: 写 spec**

```typescript
// src/views/ai/routing-feedback/__tests__/index.spec.ts
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import RoutingFeedback from '../index.vue';

const summaryMock = vi.fn();
const listMock = vi.fn();

vi.mock('@/service/api', () => ({
  fetchRoutingFeedbackSummary: (...args: any[]) => {
    summaryMock(...args);
    return Promise.resolve({
      error: null,
      data: {
        days: 7,
        total: 5,
        correct: 2,
        wrong: 3,
        wrongRate: 0.6,
        topWrongAgents: [
          {
            agentCode: 'role_mgmt',
            agentName: 'Role Mgmt',
            wrongCount: 2,
            topCorrected: { code: 'user_mgmt', name: 'User', count: 2 }
          }
        ]
      }
    });
  },
  fetchRoutingFeedbackList: (...args: any[]) => {
    listMock(...args);
    return Promise.resolve({
      error: null,
      data: {
        records: [
          {
            feedbackId: '1',
            messageId: '1',
            userId: '1',
            userName: 'tester',
            originalAgent: 'role_mgmt',
            originalAgentName: 'Role',
            feedback: 'wrong',
            correctedAgent: 'user_mgmt',
            correctedAgentName: 'User',
            traceId: 'abc12345ef',
            createTime: '2026-07-30T10:00:00Z'
          }
        ],
        total: 1,
        current: 1,
        size: 20
      }
    });
  }
}));

vi.mock('@/locales', () => ({
  $t: (k: string) => k
}));

describe('routing-feedback dashboard', () => {
  beforeEach(() => {
    summaryMock.mockClear();
    listMock.mockClear();
  });

  it('挂载时并行触发 summary + list 拉取', async () => {
    mount(RoutingFeedback, {
      global: {
        stubs: {
          NCard: { template: '<div><slot/></div>' },
          NRadioGroup: { template: '<div><slot/></div>' },
          NRadio: true,
          NGrid: { template: '<div><slot/></div>' },
          NGi: { template: '<div><slot/></div>' },
          NStatistic: true,
          NDataTable: true,
          NForm: { template: '<div><slot/></div>' },
          NFormItem: { template: '<div><slot/></div>' },
          NInput: true
        }
      }
    });
    await flushPromises();
    expect(summaryMock).toHaveBeenCalledWith(7);
    expect(listMock).toHaveBeenCalled();
  });

  it('切换时间范围 7↔30 触发 summary + list 重新拉取', async () => {
    const wrapper = mount(RoutingFeedback, {
      global: {
        stubs: {
          NCard: { template: '<div><slot/></div>' },
          NRadioGroup: { template: '<div><slot/></div>' },
          NRadio: true,
          NGrid: { template: '<div><slot/></div>' },
          NGi: { template: '<div><slot/></div>' },
          NStatistic: true,
          NDataTable: true,
          NForm: { template: '<div><slot/></div>' },
          NFormItem: { template: '<div><slot/></div>' },
          NInput: true
        }
      }
    });
    await flushPromises();
    summaryMock.mockClear();
    listMock.mockClear();
    // 切到 30 天
    (wrapper.vm as any).days = 30;
    await flushPromises();
    expect(summaryMock).toHaveBeenCalledWith(30);
    expect(listMock).toHaveBeenCalled();
  });
});
```

- [x] **Step 2: 跑 vitest**

```bash
cd hohu-admin-web && pnpm vitest run src/views/ai/routing-feedback/__tests__/index.spec.ts
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin-web && git add src/views/ai/routing-feedback/__tests__/ && git commit -m "test(ai): add routing feedback dashboard vitest spec"
```

---

## Phase C: E2E + Spec 回写（Tasks 22-23）

### Task 22: E2E Playwright 4 场景

**Files:**
- Create: `hohu-admin-web/tests/e2e/ai-admin.spec.ts`（路径按现有 e2e 目录约定调整）

> 实施期先 `ls hohu-admin-web/tests/` 找现有 e2e 测试目录约定。

- [x] **Step 1: 写 4 个 E2E 测试**

```typescript
// tests/e2e/ai-admin.spec.ts
import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  // 登录 R_SUPER 用户
  await page.goto('http://localhost:9527/login');
  await page.fill('input[placeholder*="用户名"]', 'admin');
  await page.fill('input[placeholder*="密码"]', 'admin123');
  await page.click('button[type=submit]');
  await page.waitForURL('**/home**');
});

test('管理员编辑 Agent description', async ({ page }) => {
  await page.goto('/ai/agent');
  await page.waitForSelector('text=User Mgmt');
  await page.click('tr:has-text("user_mgmt") button:has-text("编辑")');
  await page.waitForSelector('.n-drawer');
  // 改 description
  const descTextarea = page.locator('textarea').first();
  await descTextarea.fill('A'.repeat(80));
  await page.click('button:has-text("确认")');
  await expect(page.locator('.n-message')).toContainText('成功');
});

test('管理员切换 Agent enabled', async ({ page }) => {
  await page.goto('/ai/agent');
  await page.click('tr:has-text("shared") button:has-text("编辑")');
  const switchEl = page.locator('.n-switch').first();
  const beforeState = await switchEl.getAttribute('aria-checked');
  await switchEl.click();
  await page.click('button:has-text("确认")');
  await expect(page.locator('.n-message')).toContainText('成功');
  // 重新打开
  await page.click('tr:has-text("shared") button:has-text("编辑")');
  const afterState = await page.locator('.n-switch').first().getAttribute('aria-checked');
  expect(afterState).not.toBe(beforeState);
});

test('管理员绑定 Role → Agent', async ({ page }) => {
  await page.goto('/system/role');
  await page.click('tr:has-text("editor") button:has-text("AI Agent 授权")');
  await page.waitForSelector('.n-modal');
  // 勾 user_mgmt + config_mgmt
  await page
    .locator('.n-checkbox:has-text("user_mgmt") input')
    .check();
  await page
    .locator('.n-checkbox:has-text("config_mgmt") input')
    .check();
  await page.click('.n-modal button:has-text("确认")');
  await expect(page.locator('.n-message')).toContainText('成功');
});

test('反馈仪表盘', async ({ page }) => {
  await page.goto('/ai/routing-feedback');
  // 切 7 天
  await page.click('label:has-text("近 7 天")');
  await page.waitForSelector('.n-statistic');
  // 切 30 天
  await page.click('label:has-text("近 30 天")');
  await expect(page.locator('.n-statistic').first()).toBeVisible();
});
```

- [x] **Step 2: 跑 e2e**

```bash
cd hohu-admin-web && pnpm playwright test tests/e2e/ai-admin.spec.ts
```

- [x] **Step 3: Commit**

```bash
cd hohu-admin-web && git add tests/e2e/ai-admin.spec.ts && git commit -m "test(ai): add admin ui e2e scenarios"
```

---

### Task 23: 回写 spec + Ship 记录块

**Files:**
- Modify: `hohu-admin/docs/superpowers/specs/2026-07-30-multi-agent-admin-ui-design.md`

- [x] **Step 1: 改 Status + 加 Ship 记录**

```markdown
**Status**: ✅ Plan 已完成（YYYY-MM-DD）

**Ship 记录块**：

| 项 | 值 |
|---|---|
| 实施 commit (backend) | `<后端 commit hash>` |
| 实施 commit (frontend) | `<前端 commit hash>` |
| spec commit | `6e93ba8` |
| 实施日期 | YYYY-MM-DD |
| 决策数 | 27 |
| 后端测试 | 3 文件 / N 用例（详见 §9.1） |
| 前端测试 | 3 vitest + 4 E2E |
| 验证 | `pytest tests/modules/ai/ -v` 全绿 + `pnpm typecheck && pnpm lint` 通过 |
```

- [x] **Step 2: 改 §13 Phase 1 状态块**

把所有 `- [ ]` 改为 `- [x]`，文末加完成日期。

- [x] **Step 3: Commit spec rewrite**

```bash
cd hohu-admin && git add docs/superpowers/specs/2026-07-30-multi-agent-admin-ui-design.md && git commit -m "docs(ai): mark multi-agent admin ui spec as completed"
```

---

## Self-Review 检查

实施完成后，对照下表确认：

| 检查项 | 责任 task |
|---|---|
| §4.1 端点矩阵 7 个全部实现 | Task 2-10 |
| §6.1 Agent admin 全字段校验 | Task 1, 4 |
| §6.2 Routing feedback KPI + 排行 + 明细 | Task 9, 10 |
| §6.3 Role-Agent GET/PUT + 软禁用 normalize | Task 6, 7 |
| §10 决策 #1-#27 全部有回归测试 | Task 2-10 |
| §11 实施步骤 13 步全覆盖 | Task 1-23 |
| §12 参考借鉴文件全部 Read 验证 | 各 task 实施时 |
| §13 Phase 1 checklist 全勾 | Task 23 |
| 后端覆盖率 ≥ 70% | Task 11 |
| 前端 lint + typecheck 全过 | Task 15-18 |

---

## 实施完成后

- [x] 跑两个仓库全量 lint + test
- [x] 前后端联调（启动后端 :8000 + 前端 :9527，手动跑 §9.3 E2E 4 场景）
- [x] 回写 spec（Task 23）
- [x] 创建 PR（前后端各一个，遵循 DCO）
