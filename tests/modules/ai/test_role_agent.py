"""Role-Agent binding GET tests (spec §6.3, §9.1)."""

# ruff: noqa: ARG001, PLC0415  test fixture 占位参数 + 函数内 import（沿用 ai conftest 约定）

import pytest
from httpx import AsyncClient


@pytest.fixture
async def seed_role_agents():
    """Fixture：seed 一个测试 Role + 2 个 Agent + 1 条绑定（独立 committed session）.

    Plan 原始版本用 db_session.flush() + 硬编码 ID（role_id=7001, agent_id=9101/9102），
    但本地/CI 已 seed 过 shared / user_mgmt（unique code），且 db_session fixture 绑定
    outer transaction + rollback，API endpoint 的 get_db() 走连接池另一条 connection
    看不到外层事务里的写入 —— 必须用独立 AsyncSessionLocal() 真实 commit.

    本 fixture 的策略（mirror test_agent_admin.py::seed_agents）：
    1. Agent 行：复用 seed_agents 的 UPSERT 模式 —— 已存在 shared/user_mgmt 则 UPDATE
       关键字段，新增行用 next_id()；返回真实 agent_id 给测试断言用.
    2. Role 行：永远新建一个（next_id()），role_code 用 `test_role_agent_<snowflake>`
       避免并发/重复 fixture 触发 unique 约束. teardown 时按 role_id DELETE.
    3. RoleAiAgent 绑定：把上面的真实 role_id + user_mgmt agent_id 绑定，
       enabled=True；teardown 时按 role_id DELETE（role 没了绑定也跟着清）.

    返回 dict：{role_id, shared_id, user_id}（真实 Snowflake ID），测试用这些 ID
    构造 path + 断言 boundAgentIds.
    """
    from sqlalchemy import delete, select

    from app.core.id_generator import next_id
    from app.db.session import AsyncSessionLocal, engine
    from app.modules.ai.models.agent import AiAgent
    from app.modules.ai.models.role_ai_agent import RoleAiAgent
    from app.modules.system.models.role import Role

    wanted = {
        "shared": ("Shared Agent", "x" * 60, "shared prompt", "balanced"),
        "user_mgmt": ("User Mgmt", "y" * 60, "um prompt", "conservative"),
    }

    # snapshot 已存在 Agent 的原值，teardown 还原
    originals: dict[str, dict] = {}
    inserted_agent_ids: list[int] = []

    async with AsyncSessionLocal() as s:
        # 1. Agent UPSERT
        existing_map = {
            row.code: row
            for row in (
                await s.execute(select(AiAgent).where(AiAgent.code.in_(list(wanted))))
            )
            .scalars()
            .all()
        }
        resolved_agent_ids: dict[str, int] = {}
        for code, (name, desc, prompt, risk) in wanted.items():
            if code in existing_map:
                agent = existing_map[code]
                originals[code] = {
                    "agent_id": agent.agent_id,
                    "name": agent.name,
                    "description": agent.description,
                    "system_prompt": agent.system_prompt,
                    "enabled": agent.enabled,
                    "risk_appetite": agent.risk_appetite,
                }
                agent.name = name
                agent.description = desc
                agent.system_prompt = prompt
                agent.enabled = True
                agent.risk_appetite = risk
                resolved_agent_ids[code] = agent.agent_id
            else:
                new_id = next_id()
                inserted_agent_ids.append(new_id)
                s.add(
                    AiAgent(
                        agent_id=new_id,
                        code=code,
                        name=name,
                        description=desc,
                        enabled=True,
                        is_builtin=True,
                        display_order=0 if code == "shared" else 1,
                        system_prompt=prompt,
                        risk_appetite=risk,
                    )
                )
                resolved_agent_ids[code] = new_id

        # 2. 新建 Role（next_id + 唯一 role_code 避免 unique 冲突）
        role_id = next_id()
        role_code = f"test_role_agent_{role_id}"
        s.add(
            Role(
                role_id=role_id,
                role_code=role_code,
                role_name=f"Test Role Agent {role_id}",
                status="1",
            )
        )
        # 必须 flush：RoleAiAgent.role_id FK 指向 sys_role.role_id，
        # INSERT 顺序需要先让 sys_role 行可见，否则 FK 违反.
        await s.flush()

        # 3. 绑定：role_id ↔ user_mgmt agent_id（enabled=True）
        #    shared 不绑定 —— 体现 spec「shared Agent 直通，无需绑定」决策
        s.add(
            RoleAiAgent(
                role_id=role_id,
                agent_id=resolved_agent_ids["user_mgmt"],
                enabled=True,
            )
        )

        await s.commit()

    yield {
        "role_id": role_id,
        "shared_id": resolved_agent_ids["shared"],
        "user_id": resolved_agent_ids["user_mgmt"],
    }

    # teardown：精准清理新增 Role + 绑定（按 role_id），还原 / 删除 Agent 行
    async with AsyncSessionLocal() as s:
        # 绑定：role 删了绑定也会随之没意义，显式 DELETE 更稳
        await s.execute(delete(RoleAiAgent).where(RoleAiAgent.role_id == role_id))
        # Role 永远是新建的，直接删
        await s.execute(delete(Role).where(Role.role_id == role_id))
        # Agent：还原已存在 / 删除新增（mirror seed_agents）
        for _code, snap in originals.items():
            agent = await s.get(AiAgent, snap["agent_id"])
            if agent is None:
                continue
            agent.name = snap["name"]
            agent.description = snap["description"]
            agent.system_prompt = snap["system_prompt"]
            agent.enabled = snap["enabled"]
            agent.risk_appetite = snap["risk_appetite"]
        if inserted_agent_ids:
            await s.execute(
                delete(AiAgent).where(AiAgent.agent_id.in_(inserted_agent_ids))
            )
        await s.commit()

    # 释放连接池：fixture 用独立 AsyncSessionLocal 真实 commit，pool 里的连接绑到
    # 当前 loop；下个测试 setup 前必须 dispose 避免 "Event loop is closed" race
    # （参考 conftest.seed_test_message / test_executor_integration.py:99-105）
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise


async def test_get_returns_all_agents_and_bound_ids(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #2 / #14：GET 返回 allAgents（全量）+ boundAgentIds（绑定列表）.

    shared Agent 在 allAgents 中 isShared=True（决策 #14），其它 Agent isShared=False.
    boundAgentIds 只含 enabled=True 的绑定，shared 不在 boundAgentIds 中（直通无需绑定）.
    """
    client, _ = authed_client
    role_id = seed_role_agents["role_id"]
    shared_id_str = str(seed_role_agents["shared_id"])
    user_id_str = str(seed_role_agents["user_id"])

    resp = await client.get(f"/ai/role-agent/{role_id}")
    assert resp.status_code == 200, f"GET failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    # roleId 序列化为 str（Snowflake 防精度丢失）
    assert data["roleId"] == str(role_id)
    codes = {a["code"] for a in data["allAgents"]}
    assert {"shared", "user_mgmt"} <= codes
    # shared 行 isShared=True
    shared_row = next(a for a in data["allAgents"] if a["code"] == "shared")
    assert shared_row["isShared"] is True
    # 非 shared 行 isShared=False
    user_row = next(a for a in data["allAgents"] if a["code"] == "user_mgmt")
    assert user_row["isShared"] is False
    # boundAgentIds 含 user_mgmt（fixture 绑定的），不含 shared
    assert user_id_str in data["boundAgentIds"]
    assert shared_id_str not in data["boundAgentIds"]


async def test_get_excludes_soft_disabled_segment(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #19：GET 不返回 softDisabledAgentIds 段（软禁用态对前端不可见）."""
    client, _ = authed_client
    role_id = seed_role_agents["role_id"]
    resp = await client.get(f"/ai/role-agent/{role_id}")
    data = resp.json()["data"]
    # 不存在 softDisabledAgentIds 字段
    assert "softDisabledAgentIds" not in data


async def test_role_not_found_error_code_prefix(
    authed_client: tuple[AsyncClient, str], db_session
):
    """决策 #18：跨模块校验 role 不存在抛 AI_ROLE_NOT_FOUND（AI 前缀）.

    用 2**40 (1099511627776) 作「极不可能存在」的合法 int64（mirror
    test_detail_not_found 逻辑）：Snowflake ID 通常远超此值，但既不会触发
    FastAPI path 校验 (422)，又远超任何被测试覆盖的 seed 行 role_id.
    """
    client, _ = authed_client
    nonexistent_id = 2**40
    resp = await client.get(f"/ai/role-agent/{nonexistent_id}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 404
    assert body.get("errorCode") == "AI_ROLE_NOT_FOUND"
