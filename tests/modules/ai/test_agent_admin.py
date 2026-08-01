"""Multi-Agent admin UI tests (spec §6.1, §9.1)."""

# ruff: noqa: ARG001, PLC0415  test fixture 占位参数 + 函数内 import（沿用 ai conftest 约定）

import pytest
from httpx import AsyncClient


@pytest.fixture
async def seed_agents():
    """Fixture：确保 seed 的 2 行存在且字段确定（独立 committed session + UPSERT + teardown 回滚）.

    Plan 原始版本直接 INSERT 9001/9002，但 CI/本地 dev 已经 seed 过 shared /
    user_mgmt（unique code 约束），原版会触发 UniqueViolation。这里改为
    `SELECT ... WHERE code IN (...)`，缺哪个补哪个；测试断言用 code 而非 id，
    所以补种时 agent_id 用 next_id 即可（不影响断言）.

    Task 3 起对已存在行也 UPDATE 关键字段（name/description/system_prompt/
    enabled/risk_appetite）—— 否则 CI 预置的 shared 会带着空 system_prompt
    和非 `"x"*60` 的 description，让 detail/update 测试不稳定。UPSERT 后无论
    本地是否跑过 seed_ai_agents.py，断言都能稳定通过.

    **关键**：必须用独立 committed session（参考 conftest.seed_test_message），
    而非 db_session fixture —— 后者绑定 outer transaction + 强制 rollback，
    API endpoint 的 get_db() 走的是连接池另一条 connection，看不到外层事务里
    的写入。所以这里：commit 落库 → API 可见 → teardown 精准还原原值/删除新增.
    新增行用 next_id() 生成，teardown DELETE by agent_id；已存在行 teardown
    UPDATE 回原值（保留 agent_id / is_builtin / display_order 全程不动）.
    """
    from sqlalchemy import delete, select

    from app.core.id_generator import next_id
    from app.db.session import AsyncSessionLocal, engine
    from app.modules.ai.models.agent import AiAgent

    wanted = {
        "shared": ("Shared Agent", "x" * 60, "shared prompt", "balanced"),
        "user_mgmt": ("User Mgmt", "y" * 60, "um prompt", "conservative"),
    }

    # snapshot 原值用于 teardown 还原；记录新增 agent_id 用于 teardown 删除
    originals: dict[str, dict] = {}
    inserted_ids: list[int] = []

    async with AsyncSessionLocal() as s:
        existing_map = {
            row.code: row
            for row in (
                await s.execute(select(AiAgent).where(AiAgent.code.in_(list(wanted))))
            )
            .scalars()
            .all()
        }
        for code, (name, desc, prompt, risk) in wanted.items():
            if code in existing_map:
                agent = existing_map[code]
                # 快照原值（含 agent_id），teardown 还原
                originals[code] = {
                    "agent_id": agent.agent_id,
                    "name": agent.name,
                    "description": agent.description,
                    "system_prompt": agent.system_prompt,
                    "enabled": agent.enabled,
                    "risk_appetite": agent.risk_appetite,
                }
                # UPDATE 可变字段，保留 agent_id / is_builtin / display_order
                agent.name = name
                agent.description = desc
                agent.system_prompt = prompt
                agent.enabled = True
                agent.risk_appetite = risk
            else:
                new_id = next_id()
                inserted_ids.append(new_id)
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
        await s.commit()

    yield

    # teardown：还原已存在行 + 删除新增行（精准 by-id，无 TRUNCATE / 无 WHERE 1=1）
    async with AsyncSessionLocal() as s:
        for _code, snap in originals.items():
            agent = await s.get(AiAgent, snap["agent_id"])
            if agent is None:
                continue
            agent.name = snap["name"]
            agent.description = snap["description"]
            agent.system_prompt = snap["system_prompt"]
            agent.enabled = snap["enabled"]
            agent.risk_appetite = snap["risk_appetite"]
        if inserted_ids:
            await s.execute(delete(AiAgent).where(AiAgent.agent_id.in_(inserted_ids)))
        await s.commit()

    # 释放连接池：fixture 用独立 AsyncSessionLocal 真实 commit，pool 里的连接
    # 绑到当前 loop；下个测试 setup 前必须 dispose 避免 "Event loop is closed"
    # race（参考 conftest.seed_test_message / test_executor_integration.py:99-105）
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise


async def _get_agent_id_by_code(client: AsyncClient, code: str) -> str:
    """通过 code 查 agent_id（str，Snowflake 已序列化）.

    Plan 原始版本硬编码 `agent_id=9001`，但实际 agent_id 由 seed_ai_agents.py
    的 next_id() 生成（Snowflake），无法预测。所有 detail/update 测试都先
    GET /ai/admin/agents 拿列表再按 code 过滤，保证断言与具体 ID 解耦.
    """
    resp = await client.get("/ai/admin/agents")
    assert resp.status_code == 200, f"list agents failed: {resp.status_code}"
    row = next(r for r in resp.json()["data"] if r["code"] == code)
    return row["agentId"]


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
    # 无分页字段（不是 PageResult）—— PageResult 是 dict，list 即证明无分页.
    assert isinstance(data, list)


async def test_list_excludes_system_prompt(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #5：list 不返回 systemPrompt."""
    client, _ = authed_client
    resp = await client.get("/ai/admin/agents")
    data = resp.json()["data"]
    for row in data:
        assert "systemPrompt" not in row


async def test_detail_returns_system_prompt(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """detail 端点返回 systemPrompt（决策 #5）。"""
    client, _ = authed_client
    # 通过 code 查 agent_id —— Snowflake ID 不可预测，硬编码会失效
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.get(f"/ai/admin/agents/{shared_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    assert data["code"] == "shared"
    # seed_agents UPSERT 覆盖 system_prompt="shared prompt"
    assert data["systemPrompt"] == "shared prompt"


async def test_detail_not_found(authed_client: tuple[AsyncClient, str], db_session):
    """决策 #6：不存在 → 404 + errorCode=AI_AGENT_NOT_FOUND."""
    client, _ = authed_client
    # 用 2**40 (1099511627776) 作为「极不可能存在」的合法 int64。
    # Snowflake ID 虽然通常远超此值，但用 next_id() 的 freshly-seeded 测试库
    # 理论上可能产生较小的 ID；2**40 既不会触发 FastAPI path 校验 (422)，
    # 又远超任何被测试覆盖的 seed 行的 agent_id，安全且稳定.
    nonexistent_id = 2**40
    resp = await client.get(f"/ai/admin/agents/{nonexistent_id}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 404
    assert body.get("errorCode") == "AI_AGENT_NOT_FOUND"


async def test_update_partial_skips_unsent_fields(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #20：partial update，未传字段保持原值。"""
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    # 仅传 enabled，不传 description
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["enabled"] is False
    # description 保持原值（seed_agents UPSERT 后 == "x" * 60）
    assert data["description"] == "x" * 60


async def test_update_code_field_ignored(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #1：code 字段被静默忽略，不报错也不变更。"""
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"code": "hacked_code", "name": "Renamed"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["code"] == "shared"  # 未变
    assert data["name"] == "Renamed"


# ============ Task 4: 校验规则测试（决策 #3, #20, #25） ============


async def test_update_description_too_short(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #20：description < 50 字返 400 + AI_AGENT_DESC_LENGTH_INVALID.

    Service 层显式抛 BusinessRuleException（非依赖 Pydantic ValidationError
    全局映射），保证 errorCode 精确供前端 i18n 映射.
    """
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"description": "x" * 49},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_AGENT_DESC_LENGTH_INVALID"


async def test_update_description_too_long(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #20：description > 200 字返 400."""
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"description": "x" * 201},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_AGENT_DESC_LENGTH_INVALID"


async def test_description_length_algorithm_uses_code_points(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #20：按 Python len() 计 code point，中英文同权重.

    100 个中文字 = 100 code points，应在 [50, 200] 范围内通过。
    若按 UTF-8 字节计 (100*3=300 字节) 会超过 200 失败 —— 本测试
    锁定算法语义，防止日后改成 len(s.encode('utf-8')) 的回归.
    """
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"description": "中" * 100},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["description"] == "中" * 100


async def test_model_preference_format_only_no_existence_check(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #25：model_preference 只校验 'provider:model' 格式，不校验存在性."""
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    # 假 provider/model，但格式合法（小写字母+冒号+小写字母）
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"modelPreference": "xxx:yyy"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["modelPreference"] == "xxx:yyy"


async def test_model_preference_invalid_format(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #25：model_preference 非法格式（无冒号）返 400 + AI_AGENT_MODEL_PREFERENCE_INVALID."""
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"modelPreference": "invalid_no_colon"},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_AGENT_MODEL_PREFERENCE_INVALID"


async def test_update_daily_quota_zero_returns_400(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策：daily_quota_per_user ≤ 0 返 400 + AI_AGENT_QUOTA_INVALID."""
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"dailyQuotaPerUser": 0},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_AGENT_QUOTA_INVALID"


async def test_update_daily_quota_negative_returns_400(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策：daily_quota_per_user 负值返 400 + AI_AGENT_QUOTA_INVALID."""
    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    resp = await client.put(
        f"/ai/admin/agents/{shared_id}",
        json={"dailyQuotaPerUser": -5},
    )
    assert resp.status_code == 400
    assert resp.json().get("errorCode") == "AI_AGENT_QUOTA_INVALID"


# ============ Task 5: 审计 middleware 回归测试（决策 #27） ============


async def test_put_triggers_audit_middleware(
    authed_client: tuple[AsyncClient, str], db_session, seed_agents
):
    """决策 #27：PUT /ai/admin/agents/{id} 由 AuditLogMiddleware 自动审计，无需端点内审计代码.

    Plan 假设：现有 ``app/middleware/audit_middleware.py`` 的 ``AuditLogMiddleware``
    会拦截所有 PUT/POST/DELETE/PATCH 写操作并写入 ``sys_operation_log``. 本测试验证
    该假设对 ``/ai/admin/agents/{id}`` 路径成立 —— PUT 成功后日志表多一行，
    module/ action / path / request_params 与预期一致.

    实施要点：
    1. **动态 agent_id** —— Plan 原文硬编码 ``9001``，但实际 agent_id 由 Snowflake
       ``next_id()`` 生成，无法预测. 用 ``_get_agent_id_by_code`` 解耦 ID.
    2. **不查 db_session** —— middleware 用自己的 ``AsyncSessionLocal()`` 独立 session
       写日志并 commit；``db_session`` fixture 绑定 outer transaction + rollback，
       看不到这层写入. 必须用独立 ``AsyncSessionLocal()`` session 查询.
    3. **model 类名修正** —— Plan 写 ``OperationLog``，实际类名是
       ``SysOperationLog``（``app/modules/system/models/operation_log.py``）.
    4. **审计日志 teardown** —— middleware 写入的日志行会落库（不归 db_session 管控），
       必须显式按 ``path LIKE`` 清理，避免污染后续审计相关测试.
    """
    from sqlalchemy import delete, select

    from app.db.session import AsyncSessionLocal
    from app.modules.system.models.operation_log import SysOperationLog

    client, _ = authed_client
    shared_id = await _get_agent_id_by_code(client, "shared")
    audit_path = f"/ai/admin/agents/{shared_id}"

    # 触发 PUT —— middleware 会在响应返回后用独立 session 异步写审计日志
    resp = await client.put(
        audit_path,
        json={"enabled": False, "name": "Audit Test"},
    )
    assert resp.status_code == 200, f"PUT failed: {resp.status_code} {resp.text}"

    # 重新开 session 读取 —— middleware commit 后立即可见.
    # 注意: 其他 update 测试 (test_update_partial_skips_unsent_fields /
    # test_update_description_too_long 等) 也 PUT 到同一 shared agent 路径,
    # 审计 middleware 异步写入会落库; 不能简单地 after[-1] 取最后一条 ——
    # 在并发场景下 after[-1] 可能是其他测试的 PUT body (before_count 捕获后,
    # 其他测试的 middleware write 才异步落库). 必须按 request_params 含
    # "Audit Test" 轮询过滤, 才能稳定定位本测试的日志.
    import asyncio
    import time

    new_log = None
    deadline = time.time() + 5.0
    while time.time() < deadline:
        async with AsyncSessionLocal() as s:
            candidates = [
                log
                for log in (
                    (
                        await s.execute(
                            select(SysOperationLog).where(
                                SysOperationLog.path == audit_path
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if "Audit Test" in (log.request_params or "")
            ]
        if candidates:
            new_log = candidates[-1]
            break
        await asyncio.sleep(0.1)

    assert new_log is not None, (
        "5s 内未在审计日志中找到 request_params 含 'Audit Test' 的记录 "
        "(middleware 异步写入可能延迟)"
    )
    # 用 try/finally 包裹断言：任一断言失败时仍清理本测试产生的审计日志，
    # 避免污染后续审计相关测试（middleware 写入不归 db_session outer-rollback 管控）.
    try:
        assert new_log.module == "ai"
        assert new_log.action == "update"
        assert new_log.method == "PUT"
        assert audit_path in new_log.path
        # request_params 应含 PUT body 全量（middleware 不脱敏 name/enabled）
        params = new_log.request_params or ""
        assert "Audit Test" in params
        assert "enabled" in params
    finally:
        # teardown：按精确 path 删除本测试产生的审计日志.
        async with AsyncSessionLocal() as s:
            await s.execute(
                delete(SysOperationLog).where(SysOperationLog.path == audit_path)
            )
            await s.commit()
