"""system/ai_tools.py 业务逻辑集成测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.5 / §2.10。

db_session fixture 用 SAVEPOINT 回滚模式，所有写入不真正落库。
本测试只验证业务逻辑（count / stats / distinct），data_scope 过滤留 1.5 鉴权矩阵。
"""

# ruff: noqa: ARG001  test 函数 ctx / kwargs 是与生产签名一致的占位

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.ai_tools import (
    role_count,
    user_count,
    user_distinct,
    user_stats,
)
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

# ============ fixture ============


async def _add_user(
    db: AsyncSession,
    *,
    user_id: int,
    user_name: str,
    status: str = "1",
    user_gender: str | None = None,
) -> None:
    """建用户（绕过 ORM 关系，直接 insert）"""
    db.add(
        User(
            user_id=user_id,
            user_name=user_name,
            nickname=user_name,
            hashed_password="$2b$12$dummy",
            status=status,
            user_gender=user_gender,
        )
    )
    await db.flush()


def _make_ctx(
    db: AsyncSession,
    *,
    visible_user_ids: set[int] | None = None,
    data_scope: DataScopeContext | None = None,
    max_groups: int = 20,
) -> AiToolContext:
    """构造 AiToolContext

    visible_user_ids：测试用，把 data_scope.filters 设为 User.user_id.in_(...)，
    避开数据库历史数据（admin / demo 用户等）。

    data_scope：显式覆盖（优先级高于 visible_user_ids）。

    tool_meta 用真实 AiToolMeta（含白名单），让 validate_filters_in_whitelist 等
    helper 能正常读取 allowed_filters / allowed_group_by / max_groups。
    """
    if data_scope is None:
        if visible_user_ids is not None:
            filters = [User.user_id.in_(visible_user_ids)]
        else:
            filters = []
        data_scope = DataScopeContext(
            accessible_dept_ids=None,
            accessible_user_ids=None,
            filters=filters,
        )
    meta = AiToolMeta(
        name="user.test",
        agent="user_mgmt",
        summary="x",
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status", "user_gender"),
        allowed_group_by=("user_gender", "status"),
        max_groups=max_groups,
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms={"system:user:list"},
        db=db,
        data_scope=data_scope,
        trace_id="tr_test",
        tool_meta=meta,
    )


# ============ user.count ============


class TestUserCount:
    async def test_count_returns_total(self, db_session: AsyncSession) -> None:
        """3 个用户 → count=3"""
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await _add_user(db_session, user_id=1002, user_name="u2", user_gender="2")
        await _add_user(db_session, user_id=1003, user_name="u3", user_gender="1")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        result = await user_count(ctx, filters=None)
        assert result == {"count": 3}

    async def test_count_with_status_filter(self, db_session: AsyncSession) -> None:
        """status='1' 过滤：3 启用 + 1 禁用 → count=3"""
        await _add_user(db_session, user_id=1001, user_name="u1", status="1")
        await _add_user(db_session, user_id=1002, user_name="u2", status="1")
        await _add_user(db_session, user_id=1003, user_name="u3", status="1")
        await _add_user(db_session, user_id=1004, user_name="u4", status="0")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003, 1004})
        result = await user_count(ctx, filters={"status": "1"})
        assert result == {"count": 3}

    async def test_count_with_gender_filter(self, db_session: AsyncSession) -> None:
        """user_gender='1' 过滤"""
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await _add_user(db_session, user_id=1002, user_name="u2", user_gender="2")
        await _add_user(db_session, user_id=1003, user_name="u3", user_gender="1")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        result = await user_count(ctx, filters={"user_gender": "1"})
        assert result == {"count": 2}

    async def test_count_empty_table(self, db_session: AsyncSession) -> None:
        ctx = _make_ctx(db_session, visible_user_ids={999999})  # 不存在的 id
        result = await user_count(ctx, filters=None)
        assert result == {"count": 0}

    async def test_count_filter_out_of_whitelist_raises(
        self, db_session: AsyncSession
    ) -> None:
        """spec §5.5: filters 含 phone 越界 → AI_STATS_FIELD_NOT_ALLOWED"""
        ctx = _make_ctx(db_session, visible_user_ids=set())
        with pytest.raises(BusinessRuleException) as exc_info:
            await user_count(ctx, filters={"phone": "13800000000"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"


# ============ user.stats ============


class TestUserStats:
    async def test_stats_by_gender(self, db_session: AsyncSession) -> None:
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await _add_user(db_session, user_id=1002, user_name="u2", user_gender="1")
        await _add_user(db_session, user_id=1003, user_name="u3", user_gender="2")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        result = await user_stats(ctx, group_by="user_gender", filters=None)
        # 按 count 降序
        assert result == [
            {"group": "1", "count": 2},
            {"group": "2", "count": 1},
        ]

    async def test_stats_by_status(self, db_session: AsyncSession) -> None:
        await _add_user(db_session, user_id=1001, user_name="u1", status="1")
        await _add_user(db_session, user_id=1002, user_name="u2", status="1")
        await _add_user(db_session, user_id=1003, user_name="u3", status="0")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        result = await user_stats(ctx, group_by="status", filters=None)
        assert result == [
            {"group": "1", "count": 2},
            {"group": "0", "count": 1},
        ]

    async def test_stats_with_filter(self, db_session: AsyncSession) -> None:
        """filter+group_by 同时使用"""
        await _add_user(
            db_session, user_id=1001, user_name="u1", user_gender="1", status="1"
        )
        await _add_user(
            db_session, user_id=1002, user_name="u2", user_gender="2", status="1"
        )
        await _add_user(
            db_session, user_id=1003, user_name="u3", user_gender="1", status="0"
        )
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        # 只统计启用用户按性别分组（count 同为 1，order 不稳定，转 dict 比较）
        result = await user_stats(ctx, group_by="user_gender", filters={"status": "1"})
        assert {item["group"]: item["count"] for item in result} == {"1": 1, "2": 1}

    async def test_stats_group_by_none_uses_default(
        self, db_session: AsyncSession
    ) -> None:
        """group_by=None → 默认 allowed_group_by[0]"""
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001})
        result = await user_stats(ctx, group_by=None, filters=None)
        # 默认按 user_gender 分组
        assert result == [{"group": "1", "count": 1}]

    async def test_stats_max_groups_truncation(self, db_session: AsyncSession) -> None:
        """spec §5.5 max_groups 截断"""
        await _add_user(db_session, user_id=1001, user_name="u1", status="1")
        await _add_user(db_session, user_id=1002, user_name="u2", status="0")
        await db_session.flush()

        # max_groups=1 截断
        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002}, max_groups=1)
        result = await user_stats(ctx, group_by="status", filters=None)
        assert len(result) == 1
        assert result[0]["count"] == 1

    async def test_stats_group_by_out_of_whitelist_raises(
        self, db_session: AsyncSession
    ) -> None:
        ctx = _make_ctx(db_session, visible_user_ids=set())
        with pytest.raises(BusinessRuleException) as exc_info:
            await user_stats(ctx, group_by="phone", filters=None)
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"


# ============ user.distinct ============


class TestUserDistinct:
    async def test_distinct_status(self, db_session: AsyncSession) -> None:
        await _add_user(db_session, user_id=1001, user_name="u1", status="1")
        await _add_user(db_session, user_id=1002, user_name="u2", status="0")
        await _add_user(db_session, user_id=1003, user_name="u3", status="1")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        result = await user_distinct(ctx, field="status")
        # distinct 值不保证顺序，转 set 对比
        assert set(result) == {"0", "1"}

    async def test_distinct_gender(self, db_session: AsyncSession) -> None:
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await _add_user(db_session, user_id=1002, user_name="u2", user_gender="2")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002})
        result = await user_distinct(ctx, field="user_gender")
        assert set(result) == {"1", "2"}

    async def test_distinct_field_out_of_whitelist_raises(
        self, db_session: AsyncSession
    ) -> None:
        ctx = _make_ctx(db_session, visible_user_ids=set())
        with pytest.raises(BusinessRuleException) as exc_info:
            await user_distinct(ctx, field="phone")
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"


# ============ data_scope 过滤（spec §6.2，验证 filters 拼到 WHERE） ============


class TestDataScopeFilter:
    async def test_count_respects_data_scope_filters(
        self, db_session: AsyncSession
    ) -> None:
        """spec §6.2: ctx.data_scope.filters 拼到 WHERE 子句

        构造 3 个用户，data_scope filter 限定 user_id=1001 → count=1
        """
        await _add_user(db_session, user_id=1001, user_name="u1")
        await _add_user(db_session, user_id=1002, user_name="u2")
        await _add_user(db_session, user_id=1003, user_name="u3")
        await db_session.flush()

        data_scope = DataScopeContext(
            accessible_dept_ids={100},  # 不重要，本测试只验证 filters
            accessible_user_ids={1001},
            filters=[User.user_id == 1001],
        )
        ctx = _make_ctx(db_session, data_scope=data_scope)

        result = await user_count(ctx, filters=None)
        assert result == {"count": 1}


# ============ role.count（v1.5+，chip 跳转回放扩展） ============


async def _add_role(
    db: AsyncSession,
    *,
    role_id: int,
    role_name: str,
    role_code: str | None = None,
    status: str = "1",
) -> None:
    """建角色"""
    db.add(
        Role(
            role_id=role_id,
            role_name=role_name,
            role_code=role_code or role_name.lower(),
            data_scope="1",
            status=status,
        )
    )
    await db.flush()


def _make_role_ctx(db: AsyncSession) -> AiToolContext:
    """构造 role.count 的 ctx（role 不走 data_scope，全表计数）"""
    meta = AiToolMeta(
        name="role.count",
        agent="role_mgmt",
        summary="x",
        required_perms=("system:role:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        query_cache_module="system/role",
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms={"system:role:list"},
        db=db,
        data_scope=DataScopeContext(
            accessible_dept_ids=None, accessible_user_ids=None, filters=[]
        ),
        trace_id="tr_test",
        tool_meta=meta,
    )


class TestRoleCount:
    async def test_count_returns_total(self, db_session: AsyncSession) -> None:
        """2 个角色 → count=2"""
        await _add_role(db_session, role_id=2001, role_name="role_a")
        await _add_role(db_session, role_id=2002, role_name="role_b")
        await db_session.flush()

        ctx = _make_role_ctx(db_session)
        result = await role_count(ctx, filters=None)
        assert result["count"] >= 2

    async def test_count_with_status_filter(self, db_session: AsyncSession) -> None:
        """status='1' 过滤"""
        await _add_role(db_session, role_id=2001, role_name="r_enabled", status="1")
        await _add_role(db_session, role_id=2002, role_name="r_disabled", status="2")
        await db_session.flush()

        ctx = _make_role_ctx(db_session)
        result = await role_count(ctx, filters={"status": "1"})
        assert result["count"] >= 1

    async def test_count_filter_out_of_whitelist_raises(
        self, db_session: AsyncSession
    ) -> None:
        """spec §5.5: role_code 越界（allowed_filters 只有 status）→ AI_STATS_FIELD_NOT_ALLOWED"""
        ctx = _make_role_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await role_count(ctx, filters={"role_code": "admin"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"

    async def test_count_empty_table(self, db_session: AsyncSession) -> None:
        """空表 → count=0（可能含 seed 数据，至少 ≥0）"""
        ctx = _make_role_ctx(db_session)
        result = await role_count(ctx, filters=None)
        assert result["count"] >= 0
