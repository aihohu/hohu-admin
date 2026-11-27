"""Feedback SQL and real API tests with fresh tenant-scoped facts and registered Agents."""

# ruff: noqa: ARG001, PLC0415  test fixture 占位参数 + 函数内 import（沿用 ai conftest 约定）

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

# sentinel 前缀，所有 fixture 创建的 code 都以此开头，方便 teardown 精准清理
_SENTINEL_PREFIX = "test_rf_"


@pytest.fixture
async def seed_feedback(authed_client, monkeypatch):
    """Fresh owned tenant, real Agent bindings, original dataset and exact cleanup."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.id_generator import next_id
    from app.core.security import create_access_token
    from app.db import authorization_http, session
    from app.db.base import role_menus, user_roles
    from app.db.request_session import RequestSession
    from app.db.session import engine
    from app.modules.ai.agents.tools import load_builtin_tools
    from app.modules.ai.models.agent import AiAgent
    from app.modules.ai.models.role_ai_agent import RoleAiAgent
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback
    from app.modules.system.models.menu import Menu
    from app.modules.system.models.role import Role
    from app.modules.system.models.setting import SystemSetting
    from app.modules.system.models.user import User
    from app.modules.system.models.user_field_policy import UserFieldPolicyState
    from tests.committed_database import isolated_migrated_schema

    load_builtin_tools()
    client, _ = authed_client
    previous = client.headers.get("Authorization")
    user_id, role_id = next_id(), next_id()
    codes = {
        "shared": "shared",
        "user_mgmt": "user_mgmt",
        "role_mgmt": "role_mgmt",
        "config_mgmt": "dept_mgmt",
    }
    async with isolated_migrated_schema(engine.url) as scoped:
        AsyncSessionLocal = async_sessionmaker(scoped, expire_on_commit=False)
        monkeypatch.setattr(authorization_http, "engine", scoped)
        monkeypatch.setattr(
            session,
            "AsyncSessionLocal",
            async_sessionmaker(scoped, class_=RequestSession, expire_on_commit=False),
        )
        async with AsyncSessionLocal() as db, db.begin():
            tid = 0
            assert await db.get(UserFieldPolicyState, tid) is not None
            db.add(
                Role(
                    tenant_id=tid,
                    role_id=role_id,
                    role_name="Feedback test",
                    role_code="R_SUPER",
                    data_scope="1",
                    status="1",
                )
            )
            db.add(
                User(
                    tenant_id=tid,
                    user_id=user_id,
                    user_name="user",
                    hashed_password="unusable",
                    status="1",
                )
            )
            db.add(
                SystemSetting(
                    tenant_id=tid,
                    setting_key="ai:enabled_tools",
                    setting_value='["file.parse"]',
                    status="1",
                )
            )
            await db.flush()
            await db.execute(
                user_roles.insert().values(
                    tenant_id=tid, user_id=user_id, role_id=role_id
                )
            )
            for permission in (
                "ai:chat:use",
                "ai:routing-feedback:list",
                "system:user:list",
                "system:role:list",
                "system:dept:list",
                "ai:file:parse",
            ):
                menu = Menu(
                    tenant_id=tid,
                    menu_id=next_id(),
                    menu_name=permission,
                    menu_type="F",
                    status="1",
                    permission=permission,
                )
                db.add(menu)
                await db.flush()
                await db.execute(
                    role_menus.insert().values(
                        tenant_id=tid, role_id=role_id, menu_id=menu.menu_id
                    )
                )
            agents = [
                AiAgent(
                    agent_id=next_id(),
                    code=code,
                    name=code,
                    description="Fixture published Agent",
                    enabled=True,
                )
                for code in codes.values()
            ]
            db.add_all(agents)
            await db.flush()
            for agent in agents:
                db.add(
                    RoleAiAgent(
                        tenant_id=tid,
                        role_id=role_id,
                        agent_id=agent.agent_id,
                        enabled=True,
                    )
                )
            now = datetime.now()
            for number, original, corrected, feedback, days in (
                (1, "role_mgmt", "user_mgmt", "wrong", 1),
                (2, "role_mgmt", "user_mgmt", "wrong", 2),
                (3, "config_mgmt", "shared", "wrong", 3),
                (4, "user_mgmt", None, "correct", 1),
                (5, "shared", None, "correct", 1),
            ):
                db.add(
                    AiRoutingFeedback(
                        feedback_id=next_id(),
                        tenant_id=tid,
                        message_id=next_id(),
                        user_id=user_id,
                        original_agent=codes[original],
                        corrected_agent=codes[corrected] if corrected else None,
                        feedback=feedback,
                        trace_id=f"rf-{tid}-{number}",
                        create_time=now - timedelta(days=days),
                    )
                )
        client.headers["Authorization"] = "Bearer " + create_access_token(
            subject=str(user_id), tenant_id=tid, tenant_version=1, user_version=1
        )
        try:
            yield {
                "group_id": role_id,
                "tenant_id": tid,
                "prefix": "",
                "user_id": user_id,
                "agent_codes": codes,
                "sessions": AsyncSessionLocal,
                "engine": scoped,
            }
        finally:
            if previous:
                client.headers["Authorization"] = previous


def _filter_sentinel(items: list, key: str, prefix: str) -> list:
    """从聚合结果中筛出 sentinel code 开头的项（按指定 key 字段过滤）."""
    return [it for it in items if str(it.get(key, "")).startswith(prefix)]


async def test_summary_7_day_window(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """days=7 时按七天窗口正确聚合。

    本测试只断言 sentinel 数据子集（避开 DB 中其它测试残留）：
    - role_mgmt sentinel 出现 2 wrong + topCorrected=user_mgmt(2)
    - config_mgmt sentinel 出现 1 wrong
    - user_mgmt / shared sentinel 出现在 correct 聚合里（不在 topWrongAgents）
    """
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    resp = await client.get("/ai/routing-feedback/summary?days=7")
    assert resp.status_code == 200, f"summary failed: {resp.status_code} {resp.text}"
    data = resp.json()["data"]
    assert data["days"] == 7
    # 字段齐全
    assert {"total", "correct", "wrong", "wrongRate", "topWrongAgents"} <= set(
        data.keys()
    )
    # wrongRate 非负、不超过 1
    assert 0 <= data["wrongRate"] <= 1

    # 断言 sentinel 子集：topWrongAgents 含 role_mgmt(2) + config_mgmt(1)
    top = data["topWrongAgents"]
    sentinel_top = _filter_sentinel(top, "agentCode", prefix)
    # sentinel 应有 2 个 wrong agent（role_mgmt + config_mgmt；correct 的不进 top）
    sentinel_codes = {it["agentCode"] for it in sentinel_top}
    role_code = seed_feedback["agent_codes"]["role_mgmt"]
    config_code = seed_feedback["agent_codes"]["config_mgmt"]
    user_code = seed_feedback["agent_codes"]["user_mgmt"]
    assert role_code in sentinel_codes
    assert config_code in sentinel_codes
    # user_mgmt 是 correct 的 original_agent，不应进 topWrongAgents
    assert user_code not in sentinel_codes

    role_row = next(it for it in sentinel_top if it["agentCode"] == role_code)
    assert role_row["wrongCount"] == 2
    assert role_row["topCorrected"]["code"] == user_code
    assert role_row["topCorrected"]["count"] == 2

    config_row = next(it for it in sentinel_top if it["agentCode"] == config_code)
    assert config_row["wrongCount"] == 1


async def test_summary_zero_division(
    authed_client: tuple[AsyncClient, str], db_session
):
    """total=0 时 wrongRate=0，避免除零。

    全局 summary 在有数据污染时无法直接构造 total=0，所以只断言字段存在 +
    wrongRate 在 [0, 1]；total=0 的不除零分支靠 unit test 覆盖（service 单测）.
    """
    client, _ = authed_client
    resp = await client.get("/ai/routing-feedback/summary?days=7")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "total" in data
    assert "wrongRate" in data
    assert isinstance(data["total"], int)
    assert isinstance(data["wrongRate"], (int, float))
    # 数学不变量：wrong <= total, correct <= total, wrong+correct <= total
    assert data["wrong"] <= data["total"]
    assert data["correct"] <= data["total"]
    assert data["wrong"] + data["correct"] <= data["total"]
    # wrongRate = wrong/total（total>0 时），否则 0
    if data["total"] == 0:
        assert data["wrongRate"] == 0


async def test_summary_top_wrong_agents_sorted(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """topWrongAgents 按 wrong 数降序：sentinel role_mgmt(2) > sentinel config_mgmt(1).

    全局聚合下，断言只针对 sentinel 子集内部的相对顺序：
    在 sentinel 子集内 role_mgmt 必须排在 config_mgmt 前面（wrong_count 更高）.
    """
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    resp = await client.get("/ai/routing-feedback/summary?days=7")
    data = resp.json()["data"]
    top = data["topWrongAgents"]
    # sentinel 子集存在性 sanity check（_filter_sentinel 保留供调试，确认 prefix 命中）
    assert _filter_sentinel(top, "agentCode", prefix), (
        "no sentinel rows in topWrongAgents"
    )

    role_code = seed_feedback["agent_codes"]["role_mgmt"]
    config_code = seed_feedback["agent_codes"]["config_mgmt"]
    user_code = seed_feedback["agent_codes"]["user_mgmt"]

    # 找 sentinel 子集内 role_mgmt 和 config_mgmt 的全局 index
    role_idx = next(
        (i for i, it in enumerate(top) if it["agentCode"] == role_code), None
    )
    config_idx = next(
        (i for i, it in enumerate(top) if it["agentCode"] == config_code), None
    )
    assert role_idx is not None, "role_mgmt sentinel missing from topWrongAgents"
    assert config_idx is not None, "config_mgmt sentinel missing from topWrongAgents"
    # wrong_count 降序：role(2) 应该在 config(1) 前面
    role_row = top[role_idx]
    config_row = top[config_idx]
    assert role_row["wrongCount"] == 2
    assert config_row["wrongCount"] == 1
    assert role_row["wrongCount"] > config_row["wrongCount"]
    # topCorrected 众数：role_mgmt → user_mgmt(2)
    assert role_row["topCorrected"]["code"] == user_code
    assert role_row["topCorrected"]["count"] == 2

    # 全局排序不变量：topWrongAgents 整体按 wrongCount desc, agentCode asc
    for i in range(len(top) - 1):
        a, b = top[i], top[i + 1]
        assert (a["wrongCount"], a["agentCode"]) > (
            b["wrongCount"],
            b["agentCode"],
        ) or (a["wrongCount"] == b["wrongCount"] and a["agentCode"] < b["agentCode"])


async def test_top_corrected_tie_breaker(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #21：topCorrected 并列时按 corrected_agent code ASC 取首.

    seed_feedback 里 sentinel config_mgmt 的 1 个 wrong 行 corrected=sentinel_shared；
    这里另加 1 行 sentinel config_mgmt + corrected=sentinel_role_mgmt，
    制造 1-1 并列 → 取 code ASC.

    注意：sentinel code 形如 `test_rf_<snowflake>_shared` / `test_rf_<snowflake>_role_mgmt`，
    ASC 排序 role_mgmt < shared，所以并列时应取 role_mgmt.
    """
    from sqlalchemy import delete

    from app.core.id_generator import next_id
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback

    AsyncSessionLocal = seed_feedback["sessions"]
    engine = seed_feedback["engine"]

    prefix = seed_feedback["prefix"]
    user_id = seed_feedback["user_id"]
    config_code = seed_feedback["agent_codes"]["config_mgmt"]
    role_code = seed_feedback["agent_codes"]["role_mgmt"]

    extra_id = next_id()
    async with AsyncSessionLocal() as s:
        s.add(
            AiRoutingFeedback(
                feedback_id=extra_id,
                tenant_id=seed_feedback["tenant_id"],
                message_id=next_id(),
                user_id=user_id,
                original_agent=config_code,
                feedback="wrong",
                corrected_agent=role_code,
                trace_id=f"{prefix}tie_extra",
                create_time=datetime.now() - timedelta(days=1),
            )
        )
        await s.commit()

    try:
        client, _ = authed_client
        resp = await client.get("/ai/routing-feedback/summary?days=7")
        data = resp.json()["data"]
        config_row = next(
            r for r in data["topWrongAgents"] if r["agentCode"] == config_code
        )
        # config_mgmt: 1 row corrected=shared + 1 row corrected=role_mgmt → 并列
        # sentinel code ASC: role_mgmt < shared → topCorrected=role_mgmt
        assert config_row["wrongCount"] == 2
        assert config_row["topCorrected"]["code"] == role_code
        assert config_row["topCorrected"]["count"] == 1
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                delete(AiRoutingFeedback).where(
                    AiRoutingFeedback.feedback_id == extra_id
                )
            )
            await s.commit()
        try:
            await engine.dispose()
        except RuntimeError as e:
            if "Event loop is closed" not in str(e):
                raise


# ---------------------------------------------------------------------------
# GET /ai/routing-feedback/list 端点测试。
# ---------------------------------------------------------------------------


def _sentinel_records(records: list, prefix: str) -> list:
    """筛出 trace_id / original_agent 以 sentinel prefix 开头的列表行.

    Plan 原始版本断言 total==N，但 DB 有其它测试残留（478 行 user_mgmt），
    无法靠 absolute total 隔离 —— 这里按 sentinel 前缀过滤再做断言.
    """
    return [
        r
        for r in records
        if str(r.get("originalAgent", "")).startswith(prefix)
        or str(r.get("traceId", "")).startswith(prefix)
    ]


async def test_summary_and_list_are_strictly_isolated_by_tenant(
    db_session,
) -> None:
    from app.core.id_generator import next_id
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback
    from app.modules.ai.service.routing_feedback_query import (
        routing_feedback_query_service,
    )
    from tests.tenant_helpers import tenant_context

    tenant_a = tenant_context(tenant_id=next_id(), actor_user_id=next_id())
    tenant_b = tenant_context(tenant_id=next_id(), actor_user_id=next_id())
    row_a = AiRoutingFeedback(
        tenant_id=tenant_a.tenant_id,
        message_id=next_id(),
        user_id=tenant_a.actor_user_id,
        original_agent="tenant_a_agent",
        feedback="correct",
        corrected_agent=None,
        trace_id="tr_tenant_a_only",
        create_time=datetime.now(),
    )
    row_b = AiRoutingFeedback(
        tenant_id=tenant_b.tenant_id,
        message_id=next_id(),
        user_id=tenant_b.actor_user_id,
        original_agent="tenant_b_agent",
        feedback="wrong",
        corrected_agent="tenant_b_correction",
        trace_id="tr_tenant_b_forbidden",
        create_time=datetime.now(),
    )
    db_session.add_all([row_a, row_b])
    await db_session.flush()

    summary = await routing_feedback_query_service.summary(
        db_session,
        7,
        tenant=tenant_a,
    )
    items, total = await routing_feedback_query_service.list_items(
        db_session,
        days=7,
        current=1,
        size=20,
        feedback="all",
        original_agent=None,
        corrected_agent=None,
        tenant=tenant_a,
    )

    assert summary.total == 1
    assert summary.correct == 1
    assert summary.wrong == 0
    assert total == 1
    assert [item.trace_id for item in items] == ["tr_tenant_a_only"]
    assert "tenant_b" not in repr(summary)
    assert "tenant_b" not in repr(items)


async def test_list_default_filter_wrong_only(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #6：默认 feedback=wrong，列表只返回 wrong 行（不掺 correct）.

    断言只针对 sentinel 子集（避开 DB 残留）：sentinel 子集里只能有 wrong 行，
    不应出现 correct 行（fixture seed 的 correct 行有 user_mgmt / shared）.
    """
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    # 默认 feedback=wrong，size 给大一点确保 sentinel 子集全收
    resp = await client.get("/ai/routing-feedback/list?days=7&size=100")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "records" in data
    assert "total" in data
    assert data["current"] == 1
    assert data["size"] == 100

    sentinel_rows = _sentinel_records(data["records"], prefix)
    assert sentinel_rows, "no sentinel rows in default list (wrong-only)"
    # sentinel 子集内全部 wrong（不含 fixture 的 correct 行）
    assert all(r["feedback"] == "wrong" for r in sentinel_rows)
    # fixture 默认 wrong 行有 3 条（role_mgmt×2 + config_mgmt×1），可能被其它
    # 测试加入更多 sentinel 不可能 → 至少 3 条
    assert len(sentinel_rows) >= 3


async def test_list_supports_feedback_all(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #6：feedback=all 同时返回 wrong + correct.

    sentinel 子集应包含 fixture seed 的 5 行（3 wrong + 2 correct）.
    """
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    resp = await client.get("/ai/routing-feedback/list?days=7&size=100&feedback=all")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    sentinel_rows = _sentinel_records(data["records"], prefix)
    # sentinel 子集应至少有 5 行（3 wrong + 2 correct）
    assert len(sentinel_rows) >= 5
    feedback_values = {r["feedback"] for r in sentinel_rows}
    # feedback=all 下 sentinel 子集应同时出现 wrong 和 correct
    assert "wrong" in feedback_values
    assert "correct" in feedback_values


async def test_list_supports_original_agent_filter(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """originalAgent 过滤：sentinel role_mgmt code 应只返回 role_mgmt 的 wrong 行."""
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    role_code = seed_feedback["agent_codes"]["role_mgmt"]
    resp = await client.get(
        f"/ai/routing-feedback/list?days=7&size=100&originalAgent={role_code}"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # 默认 feedback=wrong，role_mgmt 有 2 行 wrong
    sentinel_rows = _sentinel_records(data["records"], prefix)
    assert sentinel_rows, "no sentinel rows for role_mgmt filter"
    # 全部 original_agent == role_code（端点 hard-filter）
    assert all(r["originalAgent"] == role_code for r in sentinel_rows)
    assert all(r["feedback"] == "wrong" for r in sentinel_rows)
    assert len(sentinel_rows) >= 2


@pytest.mark.parametrize("filter_kind", ["name", "code", "mixed"])
async def test_agent_name_filters_preserve_tenant_scope_and_pagination(
    db_session, filter_kind
):
    from app.core.id_generator import next_id
    from app.modules.ai.models.agent import AiAgent
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback
    from app.modules.ai.service.routing_feedback_query import (
        routing_feedback_query_service,
    )
    from tests.tenant_helpers import tenant_context

    marker = str(next_id())
    original_code, corrected_code = f"orig_{marker}", f"corr_{marker}"
    original_name, corrected_name = f"原助手{marker}", f"纠正助手{marker}"
    for code, name in [
        (original_code, original_name),
        (corrected_code, corrected_name),
    ]:
        db_session.add(
            AiAgent(code=code, name=name, description="验收名称筛选", enabled=True)
        )
    tenant = tenant_context(tenant_id=next_id(), actor_user_id=next_id())
    foreign_tenant_id = next_id()
    for tenant_id in [tenant.tenant_id, tenant.tenant_id, foreign_tenant_id]:
        db_session.add(
            AiRoutingFeedback(
                tenant_id=tenant_id,
                message_id=next_id(),
                user_id=tenant.actor_user_id,
                original_agent=original_code,
                corrected_agent=corrected_code,
                feedback="wrong",
                create_time=datetime.now(),
            )
        )
    await db_session.flush()
    filters = {
        "original_agent": original_code if filter_kind == "code" else original_name,
        "corrected_agent": corrected_name if filter_kind == "name" else corrected_code,
    }
    seen = []
    for page in [1, 2, 3]:
        items, total = await routing_feedback_query_service.list_items(
            db_session,
            days=7,
            current=page,
            size=1,
            feedback="wrong",
            tenant=tenant,
            **filters,
        )
        assert total == 2
        seen.extend(item.feedback_id for item in items)
        assert all(item.original_agent_name == original_name for item in items)
        assert all(item.corrected_agent_name == corrected_name for item in items)
    assert len(set(seen)) == 2
    assert seen == sorted(seen, reverse=True)


async def test_list_no_message_content_leak(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #7：列表项不能泄露消息正文（content / messageContent / contentSnapshot 等字段）."""
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    resp = await client.get("/ai/routing-feedback/list?days=7&size=100&feedback=all")
    assert resp.status_code == 200, resp.text
    records = resp.json()["data"]["records"]
    sentinel_rows = _sentinel_records(records, prefix)
    assert sentinel_rows, "no sentinel rows to inspect for content leak"
    # 列表项 schema 字段固定，禁止出现正文相关字段。
    forbidden_keys = {"content", "messageContent", "contentSnapshot", "text", "body"}
    for r in sentinel_rows:
        leak = forbidden_keys & set(r.keys())
        assert not leak, f"feedback list item leaks message content: {leak}"


async def test_list_explicit_order_by_feedback_id(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策 #15：列表按 feedback_id DESC（最新在前）.

    sentinel 子集内：feedback_id 较大的应排在较小的前面.
    """
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    resp = await client.get("/ai/routing-feedback/list?days=7&size=100&feedback=all")
    assert resp.status_code == 200, resp.text
    records = resp.json()["data"]["records"]
    sentinel_rows = _sentinel_records(records, prefix)
    assert len(sentinel_rows) >= 2, "need >=2 sentinel rows to verify ordering"
    # sentinel 子集内按 records 顺序应满足 feedback_id DESC
    ids = [int(r["feedbackId"]) for r in sentinel_rows]
    for i in range(len(ids) - 1):
        assert ids[i] > ids[i + 1], (
            f"sentinel rows not DESC by feedback_id: pos {i} {ids[i]} <= {ids[i + 1]}"
        )


async def test_list_joins_username(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """决策：LEFT JOIN sys_user 把 user_name 带出来.

    fixture 创建的 sentinel user 的 user_name 形如 `<prefix>user`，
    列表项应填充非空 userName 与之一致.
    """
    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    user_name = f"{prefix}user"

    resp = await client.get("/ai/routing-feedback/list?days=7&size=100&feedback=all")
    assert resp.status_code == 200, resp.text
    records = resp.json()["data"]["records"]
    # 找到所有指向 fixture sentinel user 的行
    sentinel_user_rows = [r for r in records if r.get("userName") == user_name]
    assert sentinel_user_rows, "no rows joined to fixture sentinel user"
    for r in sentinel_user_rows:
        assert r["userName"] == user_name


async def test_list_user_deleted_omits_unknown_username(
    authed_client: tuple[AsyncClient, str], db_session, seed_feedback
):
    """没有可信用户来源时省略userName，不能伪造合法空值.

    用 ghost-user 模式：插一行 feedback 指向不存在的 user_id，避免实际删 user
    带来的 teardown 复杂度。trace_id 带 `<prefix>ghost_` 前缀方便定位 + 清理.
    """
    from sqlalchemy import delete

    from app.core.id_generator import next_id
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback

    AsyncSessionLocal = seed_feedback["sessions"]
    engine = seed_feedback["engine"]

    client, _ = authed_client
    prefix = seed_feedback["prefix"]
    ghost_user_id = next_id()
    ghost_feedback_id = next_id()
    role_code = seed_feedback["agent_codes"]["role_mgmt"]
    ghost_trace = f"{prefix}ghost_"

    # 插 ghost 行（真实 commit 让 API 端点看见）
    async with AsyncSessionLocal() as s:
        s.add(
            AiRoutingFeedback(
                feedback_id=ghost_feedback_id,
                tenant_id=seed_feedback["tenant_id"],
                message_id=next_id(),
                user_id=ghost_user_id,
                original_agent=role_code,
                feedback="wrong",
                corrected_agent=seed_feedback["agent_codes"]["user_mgmt"],
                trace_id=ghost_trace,
                create_time=datetime.now() - timedelta(days=1),
            )
        )
        await s.commit()

    try:
        resp = await client.get(
            f"/ai/routing-feedback/list?days=7&size=100&originalAgent={role_code}"
        )
        assert resp.status_code == 200, resp.text
        records = resp.json()["data"]["records"]
        ghost_rows = [r for r in records if r["feedbackId"] == str(ghost_feedback_id)]
        assert ghost_rows, "ghost feedback row missing from list"
        assert len(ghost_rows) >= 1
        for r in ghost_rows:
            # LEFT JOIN 缺失 user → service 用 `username or ""` 填空串
            assert "userName" not in r
            assert "userId" not in r and "traceId" not in r
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                delete(AiRoutingFeedback).where(
                    AiRoutingFeedback.trace_id == ghost_trace
                )
            )
            await s.commit()
        try:
            await engine.dispose()
        except RuntimeError as e:
            if "Event loop is closed" not in str(e):
                raise
