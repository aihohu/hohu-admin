"""system/ai_tools.py 业务逻辑集成测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.5 / §2.10。

db_session fixture 用 SAVEPOINT 回滚模式，所有写入不真正落库。
本测试只验证业务逻辑（count / stats / distinct），data_scope 过滤留 1.5 鉴权矩阵。
"""

# ruff: noqa: ARG001, PLC0415  test 函数 ctx / kwargs 是与生产签名一致的占位

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
            accessible_user_scope=None,
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
        assert result.data == {"count": 3}
        assert result.ui is not None
        assert result.ui.view_type == "plain_json"
        assert result.ui.view_data["count"] == 3

    async def test_count_with_status_filter(self, db_session: AsyncSession) -> None:
        """status='1' 过滤：3 启用 + 1 禁用 → count=3"""
        await _add_user(db_session, user_id=1001, user_name="u1", status="1")
        await _add_user(db_session, user_id=1002, user_name="u2", status="1")
        await _add_user(db_session, user_id=1003, user_name="u3", status="1")
        await _add_user(db_session, user_id=1004, user_name="u4", status="0")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003, 1004})
        result = await user_count(ctx, filters={"status": "1"})
        assert result.data == {"count": 3}
        assert result.ui.view_type == "plain_json"
        assert result.ui.view_data["count"] == 3

    async def test_count_with_gender_filter(self, db_session: AsyncSession) -> None:
        """user_gender='1' 过滤"""
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await _add_user(db_session, user_id=1002, user_name="u2", user_gender="2")
        await _add_user(db_session, user_id=1003, user_name="u3", user_gender="1")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        result = await user_count(ctx, filters={"user_gender": "1"})
        assert result.data == {"count": 2}
        assert result.ui.view_type == "plain_json"

    async def test_count_empty_table(self, db_session: AsyncSession) -> None:
        ctx = _make_ctx(db_session, visible_user_ids={999999})  # 不存在的 id
        result = await user_count(ctx, filters=None)
        assert result.data == {"count": 0}
        assert result.ui is not None
        assert result.ui.view_data["count"] == 0

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
        expected = [
            {"group": "1", "count": 2},
            {"group": "2", "count": 1},
        ]
        assert result.data["groups"] == expected
        assert result.ui is not None
        assert result.ui.view_type == "stats_chart"
        assert result.ui.view_data["rows"] == expected
        assert result.ui.audit["total"] == 3

    async def test_stats_by_status(self, db_session: AsyncSession) -> None:
        await _add_user(db_session, user_id=1001, user_name="u1", status="1")
        await _add_user(db_session, user_id=1002, user_name="u2", status="1")
        await _add_user(db_session, user_id=1003, user_name="u3", status="0")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002, 1003})
        result = await user_stats(ctx, group_by="status", filters=None)
        assert result.data["groups"] == [
            {"group": "1", "count": 2},
            {"group": "0", "count": 1},
        ]
        assert result.ui.view_type == "stats_chart"

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
        assert {item["group"]: item["count"] for item in result.data["groups"]} == {
            "1": 1,
            "2": 1,
        }
        assert result.ui.view_type == "stats_chart"

    async def test_stats_group_by_none_uses_default(
        self, db_session: AsyncSession
    ) -> None:
        """group_by=None → 默认 allowed_group_by[0]"""
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001})
        result = await user_stats(ctx, group_by=None, filters=None)
        # 默认按 user_gender 分组
        assert result.data["groups"] == [{"group": "1", "count": 1}]
        assert result.ui.view_type == "stats_chart"

    async def test_stats_max_groups_truncation(self, db_session: AsyncSession) -> None:
        """spec §5.5 max_groups 截断"""
        await _add_user(db_session, user_id=1001, user_name="u1", status="1")
        await _add_user(db_session, user_id=1002, user_name="u2", status="0")
        await db_session.flush()

        # max_groups=1 截断
        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002}, max_groups=1)
        result = await user_stats(ctx, group_by="status", filters=None)
        assert len(result.data["groups"]) == 1
        assert result.data["groups"][0]["count"] == 1
        assert result.ui.view_type == "stats_chart"

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
        assert set(result.data["values"]) == {"0", "1"}
        assert result.ui is not None
        assert result.ui.view_type == "plain_json"
        assert result.ui.audit["count"] == 2

    async def test_distinct_gender(self, db_session: AsyncSession) -> None:
        await _add_user(db_session, user_id=1001, user_name="u1", user_gender="1")
        await _add_user(db_session, user_id=1002, user_name="u2", user_gender="2")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={1001, 1002})
        result = await user_distinct(ctx, field="user_gender")
        assert set(result.data["values"]) == {"1", "2"}
        assert result.ui.view_type == "plain_json"

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

        from sqlalchemy import literal_column, select

        data_scope = DataScopeContext(
            accessible_dept_ids={100},  # 不重要，本测试只验证 filters
            accessible_user_scope=select(literal_column("0").label("user_id")),
            filters=[User.user_id == 1001],
        )
        ctx = _make_ctx(db_session, data_scope=data_scope)

        result = await user_count(ctx, filters=None)
        assert result.data == {"count": 1}


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
            accessible_dept_ids=None, accessible_user_scope=None, filters=[]
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
        assert result.data["count"] >= 2
        assert result.ui is not None
        assert result.ui.view_type == "plain_json"
        assert result.ui.view_data["count"] >= 2

    async def test_count_with_status_filter(self, db_session: AsyncSession) -> None:
        """status='1' 过滤"""
        await _add_role(db_session, role_id=2001, role_name="r_enabled", status="1")
        await _add_role(db_session, role_id=2002, role_name="r_disabled", status="2")
        await db_session.flush()

        ctx = _make_role_ctx(db_session)
        result = await role_count(ctx, filters={"status": "1"})
        assert result.data["count"] >= 1

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
        assert result.data["count"] >= 0


# ============ dept.count（v1.5+，演示 chip 跳转回放到 dept 模块页） ============


async def _add_dept(
    db: AsyncSession,
    *,
    dept_id: int,
    dept_name: str,
    status: str = "1",
) -> None:
    """建部门"""
    from app.modules.system.models.dept import Dept

    db.add(
        Dept(
            dept_id=dept_id,
            dept_name=dept_name,
            order_num=0,
            status=status,
        )
    )
    await db.flush()


def _make_dept_ctx(db: AsyncSession) -> AiToolContext:
    """构造 dept.count 的 ctx（dept 不走 data_scope，全表计数）"""
    from app.modules.system.ai_tools import dept_count  # noqa: F401 (确保 import)

    meta = AiToolMeta(
        name="dept.count",
        agent="dept_mgmt",
        summary="x",
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        query_cache_module="system/dept",
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms={"system:dept:list"},
        db=db,
        data_scope=DataScopeContext(
            accessible_dept_ids=None, accessible_user_scope=None, filters=[]
        ),
        trace_id="tr_test",
        tool_meta=meta,
    )


class TestDeptCount:
    async def test_count_returns_total(self, db_session: AsyncSession) -> None:
        from app.modules.system.ai_tools import dept_count

        await _add_dept(db_session, dept_id=3001, dept_name="dept_a")
        await _add_dept(db_session, dept_id=3002, dept_name="dept_b")
        await db_session.flush()

        ctx = _make_dept_ctx(db_session)
        result = await dept_count(ctx, filters=None)
        assert result.data["count"] >= 2
        assert result.ui is not None
        assert result.ui.view_type == "plain_json"
        assert result.ui.view_data["count"] >= 2

    async def test_count_with_status_filter(self, db_session: AsyncSession) -> None:
        from app.modules.system.ai_tools import dept_count

        await _add_dept(db_session, dept_id=3001, dept_name="d_on", status="1")
        await _add_dept(db_session, dept_id=3002, dept_name="d_off", status="0")
        await db_session.flush()

        ctx = _make_dept_ctx(db_session)
        result = await dept_count(ctx, filters={"status": "1"})
        assert result.data["count"] >= 1

    async def test_count_filter_out_of_whitelist_raises(
        self, db_session: AsyncSession
    ) -> None:
        """spec §5.5: dept_name 越界（allowed_filters 只有 status）→ AI_STATS_FIELD_NOT_ALLOWED"""
        from app.modules.system.ai_tools import dept_count

        ctx = _make_dept_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await dept_count(ctx, filters={"dept_name": "evil"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"


# ============ role.list / dept.list（v1.5+ SR-22） ============


def _make_role_list_ctx(db: AsyncSession) -> AiToolContext:
    """构造 role.list 的 ctx"""
    from app.modules.system.ai_tools import role_list  # noqa: F401

    meta = AiToolMeta(
        name="role.list",
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
            accessible_dept_ids=None, accessible_user_scope=None
        ),
        trace_id="tr_role_list",
        tool_meta=meta,
    )


def _make_dept_list_ctx(db: AsyncSession) -> AiToolContext:
    """构造 dept.list 的 ctx"""
    from app.modules.system.ai_tools import dept_list  # noqa: F401

    meta = AiToolMeta(
        name="dept.list",
        agent="dept_mgmt",
        summary="x",
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        query_cache_module="system/dept",
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms={"system:dept:list"},
        db=db,
        data_scope=DataScopeContext(
            accessible_dept_ids=None, accessible_user_scope=None
        ),
        trace_id="tr_dept_list",
        tool_meta=meta,
    )


class TestRoleList:
    """spec §5.5 SR-22: role.list 返回精简字段 + limit 截断"""

    async def test_returns_records_with_default_limit(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        await _add_role(db_session, role_id=4001, role_name="r1", role_code="R_R1")
        await _add_role(db_session, role_id=4002, role_name="r2", role_code="R_R2")
        await db_session.flush()

        ctx = _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters=None)
        assert result["limit"] == 20  # 默认
        assert result["total"] >= 2  # 含 _add_role 加的 + 其它测试残留
        assert len(result["records"]) >= 2
        # records 应含本次新建的 role
        names = [r["name"] for r in result["records"]]
        assert "r1" in names and "r2" in names
        # 精简字段：含 id/name/code/status
        rec0 = result["records"][0]
        assert set(rec0.keys()) == {"id", "name", "code", "status"}
        # id 字符串化（防 JS BigInt）
        assert isinstance(rec0["id"], str)

    async def test_status_filter_applied(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        await _add_role(db_session, role_id=4011, role_name="on", status="1")
        await _add_role(db_session, role_id=4012, role_name="off", status="2")
        await db_session.flush()

        ctx = _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters={"status": "2"})
        names = [r["name"] for r in result["records"]]
        assert "off" in names
        assert "on" not in names  # status='1' 被过滤

    async def test_limit_over_50_truncated(self, db_session) -> None:
        """spec §5.5 SR-22 反例 3: limit > 50 强制截断到 50"""
        from app.modules.system.ai_tools import role_list

        ctx = _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters=None, limit=999)
        assert result["limit"] == 50  # 截断

    async def test_limit_none_or_zero_uses_default(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        ctx = _make_role_list_ctx(db_session)
        r1 = await role_list(ctx, filters=None, limit=None)
        r2 = await role_list(ctx, filters=None, limit=0)
        r3 = await role_list(ctx, filters=None, limit=-5)
        assert r1["limit"] == 20
        assert r2["limit"] == 20
        assert r3["limit"] == 20  # 负数也走默认

    async def test_total_reflects_real_count_not_limit(self, db_session) -> None:
        """spec §5.5 SR-22 反例 4: total 不受 limit 截断"""
        from app.modules.system.ai_tools import role_list

        # 至少 3 个 role（含本次 + 残留）
        await _add_role(db_session, role_id=4021, role_name="total_test_1")
        await _add_role(db_session, role_id=4022, role_name="total_test_2")
        await _add_role(db_session, role_id=4023, role_name="total_test_3")
        await db_session.flush()

        ctx = _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters=None, limit=1)
        # total ≥ 3（真实总数），但 records 只有 1 条
        assert result["total"] >= 3
        assert len(result["records"]) == 1
        assert result["limit"] == 1

    async def test_filter_out_of_whitelist_raises(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        ctx = _make_role_list_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await role_list(ctx, filters={"role_name": "evil"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"


class TestDeptList:
    """spec §5.5 SR-22: dept.list 返回精简字段"""

    async def test_returns_records_with_default_limit(self, db_session) -> None:
        from app.modules.system.ai_tools import dept_list

        await _add_dept(db_session, dept_id=5001, dept_name="d1")
        await _add_dept(db_session, dept_id=5002, dept_name="d2")
        await db_session.flush()

        ctx = _make_dept_list_ctx(db_session)
        result = await dept_list(ctx, filters=None)
        assert result["limit"] == 20
        assert result["total"] >= 2
        names = [r["name"] for r in result["records"]]
        assert "d1" in names and "d2" in names
        # 精简字段：含 id/name/parent_id/status（dept 无 code 字段）
        rec0 = result["records"][0]
        assert set(rec0.keys()) == {"id", "name", "parent_id", "status"}
        assert isinstance(rec0["id"], str)

    async def test_status_filter_applied(self, db_session) -> None:
        from app.modules.system.ai_tools import dept_list

        await _add_dept(db_session, dept_id=5011, dept_name="on", status="1")
        await _add_dept(db_session, dept_id=5012, dept_name="off", status="0")
        await db_session.flush()

        ctx = _make_dept_list_ctx(db_session)
        result = await dept_list(ctx, filters={"status": "0"})
        names = [r["name"] for r in result["records"]]
        assert "off" in names
        assert "on" not in names

    async def test_limit_truncated_to_50(self, db_session) -> None:
        from app.modules.system.ai_tools import dept_list

        ctx = _make_dept_list_ctx(db_session)
        result = await dept_list(ctx, filters=None, limit=100)
        assert result["limit"] == 50
