"""Multi-Agent admin UI tests (spec §6.1, §9.1)."""

# ruff: noqa: ARG001, PLC0415  test fixture 占位参数 + 函数内 import（沿用 ai conftest 约定）

import pytest
from httpx import AsyncClient


@pytest.fixture
async def seed_agents(db_session):
    """Fixture：确保 seed 的 2 行存在（idempotent — CI 已通过 seed_ai_agents.py 预置）.

    Plan 原始版本直接 INSERT 9001/9002，但 CI/本地 dev 已经 seed 过 shared /
    user_mgmt（unique code 约束），原版会触发 UniqueViolation。这里改为
    `SELECT ... WHERE code IN (...)`，缺哪个补哪个；测试断言用 code 而非 id，
    所以补种时 agent_id 用 next_id 即可（不影响断言）.
    """
    from sqlalchemy import select

    from app.core.id_generator import next_id
    from app.modules.ai.models.agent import AiAgent

    wanted = {
        "shared": ("Shared Agent", "x" * 60, "shared prompt", "balanced"),
        "user_mgmt": ("User Mgmt", "y" * 60, "um prompt", "conservative"),
    }
    existing = (
        (
            await db_session.execute(
                select(AiAgent.code).where(AiAgent.code.in_(list(wanted)))
            )
        )
        .scalars()
        .all()
    )
    for code, (name, desc, prompt, risk) in wanted.items():
        if code in existing:
            continue
        db_session.add(
            AiAgent(
                agent_id=next_id(),
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
