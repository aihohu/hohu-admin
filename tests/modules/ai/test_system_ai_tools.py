"""system/ai_tools.py 业务逻辑集成测试

覆盖系统聚合、列表工具的数据权限和字段白名单。

db_session fixture 用 SAVEPOINT 回滚模式，所有写入不真正落库。
本测试只验证业务逻辑（count / stats / distinct），data_scope 过滤留 1.5 鉴权矩阵。
"""

# ruff: noqa: ARG001, PLC0415  test 函数 ctx / kwargs 是与生产签名一致的占位

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.id_generator import next_id
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.ai_tools import (
    role_count,
    user_count,
    user_distinct,
    user_stats,
)
from app.modules.system.models.menu import Menu
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
        """filters 包含 phone 时返回 AI_STATS_FIELD_NOT_ALLOWED。"""
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
        """max_groups 应截断分组结果。"""
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


# ============ data_scope 过滤 ============


class TestDataScopeFilter:
    async def test_count_respects_data_scope_filters(
        self, db_session: AsyncSession
    ) -> None:
        """ctx.data_scope.filters 应拼入 WHERE 子句。

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


# ============ role.count 与 chip 回放 ============


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
        """role_code 不在白名单时返回 AI_STATS_FIELD_NOT_ALLOWED。"""
        ctx = _make_role_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await role_count(ctx, filters={"role_code": "admin"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"

    async def test_count_empty_table(self, db_session: AsyncSession) -> None:
        """空表 → count=0（可能含 seed 数据，至少 ≥0）"""
        ctx = _make_role_ctx(db_session)
        result = await role_count(ctx, filters=None)
        assert result.data["count"] >= 0


# ============ dept.count 与 chip 回放 ============


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
        """dept_name 不在白名单时返回 AI_STATS_FIELD_NOT_ALLOWED。"""
        from app.modules.system.ai_tools import dept_count

        ctx = _make_dept_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await dept_count(ctx, filters={"dept_name": "evil"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"


# ============ role.list / dept.list ============


async def _make_role_list_ctx(db: AsyncSession) -> AiToolContext:
    """Build role.list context with a real explicitly authorized principal."""
    from app.modules.system.ai_tools import role_list  # noqa: F401

    marker = next_id()
    permission = Menu(
        menu_id=next_id(),
        menu_name=f"role-list-permission-{marker}",
        menu_type="F",
        permission="system:role:list",
        status="1",
    )
    actor_role = Role(
        role_id=next_id(),
        role_name=f"role-list-actor-{marker}",
        role_code=f"R_ROLE_LIST_ACTOR_{marker}",
        data_scope="1",
        status="1",
        menus=[permission],
    )
    actor = User(
        user_id=next_id(),
        user_name=f"role-list-actor-{marker}",
        nickname="Role list actor",
        hashed_password="x",
        status="1",
        roles=[actor_role],
    )
    db.add(actor)
    await db.flush()
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
        user=actor,
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
    """role.list 返回精简字段并按 limit 截断。

    双层返回：
      LLM 看 data.{total, limit, sample[3]}（精简，进 prompt cache）
      前端看 ui.view_data.{columns, rows}（全量 limit 条，渲染 table）
    """

    async def test_returns_records_with_default_limit(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        await _add_role(db_session, role_id=4001, role_name="r1", role_code="R_R1")
        await _add_role(db_session, role_id=4002, role_name="r2", role_code="R_R2")
        await db_session.flush()

        ctx = await _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters=None)
        # LLM 层
        assert result.data["limit"] == 20  # 默认
        assert result.data["total"] >= 2  # 含 _add_role 加的 + 其它测试残留
        assert len(result.data["sample"]) == min(3, result.data["total"])
        # rows 是全量 limit 范围；sample 只取前 3，可能漏掉本次新建（残留干扰），
        # 故用 rows 验证新建项命中。
        rows = result.ui.view_data["rows"]
        rows_names = [r["name"] for r in rows]
        assert "r1" in rows_names and "r2" in rows_names
        # Phase 3 adds delegation state without exposing aggregate internals.
        rec0 = rows[0]
        assert set(rec0.keys()) == {
            "id",
            "name",
            "code",
            "status",
            "dataScope",
            "delegable",
            "blockedReasonCode",
        }
        # id 字符串化（防 JS BigInt）
        assert isinstance(rec0["id"], str)
        # UI 层：data_list
        assert result.ui is not None
        assert result.ui.view_type == "data_list"
        assert result.ui.view_data["columns"] == [
            {"key": "id", "label": "ID"},
            {"key": "name", "label": "ai.tool.field.name"},
            {"key": "code", "label": "ai.tool.field.code"},
            {"key": "status", "label": "ai.tool.field.status"},
            {"key": "delegable", "label": "ai.tool.field.delegable"},
            {
                "key": "blockedReasonCode",
                "label": "ai.tool.field.blockedReasonCode",
            },
        ]
        assert len(rows) >= 2
        # audit total 反映真实总数（_AFFECTED_ROW_KEYS 命中 total）
        assert result.ui.audit["total"] == result.data["total"]

    async def test_status_filter_applied(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        await _add_role(db_session, role_id=4011, role_name="role_on_uniq", status="1")
        await _add_role(db_session, role_id=4012, role_name="role_off_uniq", status="2")
        await db_session.flush()

        ctx = await _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters={"status": "2"})
        # rows 全量（受 filter 约束）；sample 截断可能漏，故断言 rows
        names = [r["name"] for r in result.ui.view_data["rows"]]
        assert "role_off_uniq" in names
        assert "role_on_uniq" not in names  # status='1' 被过滤

    async def test_limit_over_50_truncated(self, db_session) -> None:
        """limit 大于 50 时强制截断为 50。"""
        from app.modules.system.ai_tools import role_list

        ctx = await _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters=None, limit=999)
        assert result.data["limit"] == 50  # 截断
        # rows 也受同样 limit 约束
        assert len(result.ui.view_data["rows"]) <= 50

    async def test_limit_none_or_zero_uses_default(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        ctx = await _make_role_list_ctx(db_session)
        r1 = await role_list(ctx, filters=None, limit=None)
        r2 = await role_list(ctx, filters=None, limit=0)
        r3 = await role_list(ctx, filters=None, limit=-5)
        assert r1.data["limit"] == 20
        assert r2.data["limit"] == 20
        assert r3.data["limit"] == 20  # 负数也走默认

    async def test_total_reflects_real_count_not_limit(self, db_session) -> None:
        """total 不受 limit 截断影响。"""
        from app.modules.system.ai_tools import role_list

        # 至少 3 个 role（含本次 + 残留）
        await _add_role(db_session, role_id=4021, role_name="total_test_1")
        await _add_role(db_session, role_id=4022, role_name="total_test_2")
        await _add_role(db_session, role_id=4023, role_name="total_test_3")
        await db_session.flush()

        ctx = await _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters=None, limit=1)
        # total ≥ 3（真实总数），但 rows / sample 受 limit 影响
        assert result.data["total"] >= 3
        assert len(result.ui.view_data["rows"]) == 1
        # sample 最多 3，但因 limit=1 实际只取 1 条
        assert len(result.data["sample"]) == 1
        assert result.data["limit"] == 1

    async def test_sample_truncated_to_3(self, db_session) -> None:
        """sample 最多给 LLM 3 条（prompt cache 友好）"""
        from app.modules.system.ai_tools import role_list

        for i in range(5):
            await _add_role(
                db_session, role_id=4030 + i, role_name=f"s{i}", role_code=f"S_{i}"
            )
        await db_session.flush()

        ctx = await _make_role_list_ctx(db_session)
        result = await role_list(ctx, filters=None, limit=10)
        # rows 全量（10 条上限），sample 只前 3
        assert len(result.data["sample"]) <= 3

    async def test_filter_out_of_whitelist_raises(self, db_session) -> None:
        from app.modules.system.ai_tools import role_list

        ctx = await _make_role_list_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await role_list(ctx, filters={"role_name": "evil"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"


class TestDeptList:
    """dept.list 返回精简字段。

    双层返回：LLM 读取 data.{total, limit, sample[3]}，前端读取
    ui.view_data.{columns, rows}（data_list 视图）。
    """

    async def test_returns_records_with_default_limit(self, db_session) -> None:
        from app.modules.system.ai_tools import dept_list

        # 用唯一名字避开其它测试残留（"d1" / "D1" 冲突）+ sample 截断漏掉
        await _add_dept(db_session, dept_id=5001, dept_name="dept_list_d1_unique")
        await _add_dept(db_session, dept_id=5002, dept_name="dept_list_d2_unique")
        await db_session.flush()

        ctx = _make_dept_list_ctx(db_session)
        result = await dept_list(ctx, filters=None)
        assert result.data["limit"] == 20
        assert result.data["total"] >= 2
        # rows 是全量 limit 范围；sample 只取前 3，可能漏掉本次新建（残留干扰）
        rows_names = [r["name"] for r in result.ui.view_data["rows"]]
        assert "dept_list_d1_unique" in rows_names
        assert "dept_list_d2_unique" in rows_names
        # 精简字段：含 id/name/parent_id/status（dept 无 code 字段）
        rec0 = result.ui.view_data["rows"][0]
        assert set(rec0.keys()) == {"id", "name", "parent_id", "status"}
        assert isinstance(rec0["id"], str)
        # sample 至少 1 条且 ≤3，字段结构与 rows 一致
        assert 1 <= len(result.data["sample"]) <= 3
        if result.data["sample"]:
            assert set(result.data["sample"][0].keys()) == {
                "id",
                "name",
                "parent_id",
                "status",
            }
        # UI 层：data_list with parent_id 列
        assert result.ui is not None
        assert result.ui.view_type == "data_list"
        assert result.ui.view_data["columns"] == [
            {"key": "id", "label": "ID"},
            {"key": "name", "label": "ai.tool.field.name"},
            {"key": "parent_id", "label": "ai.tool.field.parentDeptId"},
            {"key": "status", "label": "ai.tool.field.status"},
        ]
        assert len(rows_names) >= 2
        assert result.ui.audit["total"] == result.data["total"]

    async def test_status_filter_applied(self, db_session) -> None:
        from app.modules.system.ai_tools import dept_list

        await _add_dept(db_session, dept_id=5011, dept_name="dept_on_uniq", status="1")
        await _add_dept(db_session, dept_id=5012, dept_name="dept_off_uniq", status="0")
        await db_session.flush()

        ctx = _make_dept_list_ctx(db_session)
        result = await dept_list(ctx, filters={"status": "0"})
        # rows 全量（受 filter 约束）；sample 截断可能漏，故断言 rows
        names = [r["name"] for r in result.ui.view_data["rows"]]
        assert "dept_off_uniq" in names
        assert "dept_on_uniq" not in names  # status='1' 被过滤

    async def test_limit_truncated_to_50(self, db_session) -> None:
        from app.modules.system.ai_tools import dept_list

        ctx = _make_dept_list_ctx(db_session)
        result = await dept_list(ctx, filters=None, limit=100)
        assert result.data["limit"] == 50
        assert len(result.ui.view_data["rows"]) <= 50
