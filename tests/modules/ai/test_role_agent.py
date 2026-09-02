"""Role-Agent binding API 与 service 测试。"""

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
    from app.core.tenant import DEFAULT_TENANT_ID
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
                tenant_id=DEFAULT_TENANT_ID,
                role_id=role_id,
                role_code=role_code,
                role_name=f"Test Role Agent {role_id}",
                status="1",
            )
        )
        # 必须 flush：RoleAiAgent.role_id FK 指向 sys_role.role_id，
        # INSERT 顺序需要先让 sys_role 行可见，否则 FK 违反.
        await s.flush()

        # Bind only user_mgmt so the fixture proves shared is not implicit.
        s.add(
            RoleAiAgent(
                tenant_id=DEFAULT_TENANT_ID,
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
        await s.execute(
            delete(RoleAiAgent).where(
                RoleAiAgent.tenant_id == DEFAULT_TENANT_ID,
                RoleAiAgent.role_id == role_id,
            )
        )
        # Role 永远是新建的，直接删
        await s.execute(
            delete(Role).where(
                Role.tenant_id == DEFAULT_TENANT_ID,
                Role.role_id == role_id,
            )
        )
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
    """Return every Agent and only explicitly enabled Role-Agent bindings."""
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


# ============ PUT 全量覆盖与边界测试 ============


async def _get_agent_id_by_code(db_session, code: str) -> str:
    """Resolve a global agent without crossing the platform admin boundary."""
    from sqlalchemy import select

    from app.modules.ai.models.agent import AiAgent

    agent_id = await db_session.scalar(
        select(AiAgent.agent_id).where(AiAgent.code == code)
    )
    assert agent_id is not None, f"agent not seeded: {code}"
    return str(agent_id)


async def test_put_full_replace_semantics(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #15：PUT 全量覆盖 —— 新列表完全替换旧绑定（DELETE + INSERT）.

    预置（fixture）：role 绑了 user_mgmt（enabled=True）.
    操作：PUT 提交 [role_mgmt]，原 user_mgmt 绑定应被 DELETE，只剩 role_mgmt.
    断言：GET 返回 boundAgentIds == [role_mgmt]（不含 user_mgmt，体现全量覆盖）.
    """
    client, _ = authed_client
    role_id = seed_role_agents["role_id"]
    role_mgmt_id = await _get_agent_id_by_code(db_session, "role_mgmt")
    user_id_str = str(seed_role_agents["user_id"])

    resp = await client.put(
        f"/ai/role-agent/{role_id}",
        json={"agentIds": [role_mgmt_id]},
    )
    assert resp.status_code == 200, f"PUT failed: {resp.status_code} {resp.text}"

    # GET 验证：boundAgentIds == [role_mgmt]，user_mgmt 已被覆盖掉
    get_resp = await client.get(f"/ai/role-agent/{role_id}")
    assert get_resp.status_code == 200
    bound = get_resp.json()["data"]["boundAgentIds"]
    assert bound == [role_mgmt_id]
    assert user_id_str not in bound


async def test_put_normalizes_soft_disabled_to_enabled(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #15：PUT 全量覆盖 normalize 软禁用态为 enabled=True.

    预置：通过 AsyncSessionLocal 真实 commit 插入一条 enabled=False 的
    RoleAiAgent 绑定（模拟「软禁用」历史态）.
    操作：PUT 提交该 agent_id.
    断言：PUT 后 GET boundAgentIds 含该 id（说明 enabled=True，因为 GET
    只返回 enabled=True 的绑定 —— 见 service.get_binding 第 65 行）.
    """
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.modules.ai.models.role_ai_agent import RoleAiAgent

    client, _ = authed_client
    role_id = seed_role_agents["role_id"]
    user_id = seed_role_agents["user_id"]

    # 预置：把 user_mgmt 这条绑定 UPDATE 为 enabled=False（fixture 原本 enabled=True）
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                select(RoleAiAgent).where(
                    RoleAiAgent.tenant_id == 0,
                    RoleAiAgent.role_id == role_id,
                    RoleAiAgent.agent_id == user_id,
                )
            )
        ).scalar_one()
        row.enabled = False
        await s.commit()

    # PUT 提交该 agent_id —— service 全量覆盖会 DELETE + INSERT enabled=True
    resp = await client.put(
        f"/ai/role-agent/{role_id}",
        json={"agentIds": [str(user_id)]},
    )
    assert resp.status_code == 200, f"PUT failed: {resp.status_code} {resp.text}"

    # GET 验证：boundAgentIds 含 user_id（说明 normalize 成 enabled=True）
    get_resp = await client.get(f"/ai/role-agent/{role_id}")
    assert get_resp.status_code == 200
    bound = get_resp.json()["data"]["boundAgentIds"]
    assert str(user_id) in bound


async def test_put_shared_binding_is_explicitly_supported(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """shared 是普通 Agent code，必须允许角色显式绑定。"""
    client, _ = authed_client
    role_id = seed_role_agents["role_id"]
    shared_id = seed_role_agents["shared_id"]

    resp = await client.put(
        f"/ai/role-agent/{role_id}",
        json={"agentIds": [str(shared_id)]},
    )
    assert resp.status_code == 200
    get_resp = await client.get(f"/ai/role-agent/{role_id}")
    assert get_resp.status_code == 200
    assert str(shared_id) in get_resp.json()["data"]["boundAgentIds"]


async def test_put_empty_array_unbinds_all(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #15：PUT 空数组 = 全量覆盖为空集 = 解绑全部.

    预置（fixture）：role 绑了 user_mgmt.
    操作：PUT 提交 [].
    断言：GET boundAgentIds == []（全量覆盖语义，DELETE + INSERT 0 行）.
    """
    client, _ = authed_client
    role_id = seed_role_agents["role_id"]

    resp = await client.put(
        f"/ai/role-agent/{role_id}",
        json={"agentIds": []},
    )
    assert resp.status_code == 200, f"PUT failed: {resp.status_code} {resp.text}"

    get_resp = await client.get(f"/ai/role-agent/{role_id}")
    assert get_resp.status_code == 200
    bound = get_resp.json()["data"]["boundAgentIds"]
    assert bound == []


async def test_put_triggers_audit_middleware(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """决策 #27：PUT /ai/role-agent/{id} 由 AuditLogMiddleware 自动审计.

    验证 middleware 写入 sys_operation_log（不查 db_session —— middleware 用
    独立 AsyncSessionLocal 写入，对 outer transaction 不可见）：
    - module == 'ai'（_extract_module 取 path 首段）
    - action == 'update'（PUT 在 METHOD_ACTION_MAP 映射）
    - method == 'PUT'
    - path 含 /ai/role-agent/{role_id}
    - request_params 含 PUT body（agentIds）

    teardown：显式按 path DELETE middleware 写入的日志行，避免污染后续审计
    相关测试（不归 db_session outer-rollback 管控）.
    """
    from sqlalchemy import delete, select

    from app.db.session import AsyncSessionLocal
    from app.modules.system.models.operation_log import SysOperationLog

    client, _ = authed_client
    role_id = seed_role_agents["role_id"]
    user_id_str = str(seed_role_agents["user_id"])
    audit_path = f"/ai/role-agent/{role_id}"

    # 用独立 session 读 audit log（middleware 写入对 db_session 外层事务不可见）
    async with AsyncSessionLocal() as s:
        before = (
            (
                await s.execute(
                    select(SysOperationLog).where(
                        SysOperationLog.tenant_id == 0,
                        SysOperationLog.path == audit_path,
                    )
                )
            )
            .scalars()
            .all()
        )
        before_count = len(before)

    # 触发 PUT —— middleware 在响应返回后用独立 session 异步写审计日志
    resp = await client.put(
        audit_path,
        json={"agentIds": [user_id_str]},
    )
    assert resp.status_code == 200, f"PUT failed: {resp.status_code} {resp.text}"

    # 重新开 session 读 —— middleware commit 后立即可见
    async with AsyncSessionLocal() as s:
        after = (
            (
                await s.execute(
                    select(SysOperationLog).where(
                        SysOperationLog.tenant_id == 0,
                        SysOperationLog.path == audit_path,
                    )
                )
            )
            .scalars()
            .all()
        )

        try:
            assert len(after) == before_count + 1
            new_log = after[-1]
            assert new_log.module == "ai"
            assert new_log.action == "update"
            assert new_log.method == "PUT"
            assert audit_path in new_log.path
            # request_params 应含 PUT body（middleware 不脱敏 agentIds）
            params = new_log.request_params or ""
            assert "agentIds" in params
            assert user_id_str in params
        finally:
            # teardown：清理本测试产生的审计日志，避免污染后续审计相关测试
            await s.execute(
                delete(SysOperationLog).where(
                    SysOperationLog.tenant_id == 0,
                    SysOperationLog.path == audit_path,
                )
            )
            await s.commit()


# ============ agent_ids 格式校验 ============


async def test_put_invalid_agent_id_format_returns_400(
    authed_client: tuple[AsyncClient, str], db_session, seed_role_agents
):
    """agent_ids 必须为数字字符串 —— 非数字（"abc"）返 400 + AI_AGENT_ID_INVALID.

    防回归：put_binding 不应让 ``int(aid)`` 的 ValueError 透传为 500，
    非数字字符串会抛 ValueError → 未捕获 → HTTP 500 无 errorCode. Fix A 在
    service 层 try/except 包裹，抛 BusinessRuleException（→ HTTP 400，
    app/core/exceptions.py:75 映射）.
    """
    client, _ = authed_client
    role_id = seed_role_agents["role_id"]

    resp = await client.put(
        f"/ai/role-agent/{role_id}",
        json={"agentIds": ["abc"]},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_AGENT_ID_INVALID"
