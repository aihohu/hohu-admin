"""system/ai_tools.py user.list / user.lookup / user.update 集成测试

覆盖用户列表、查找和更新 AI 工具。

db_session fixture 用 outer-transaction 回滚模式，所有写入不真正落库。
测试覆盖：
  - user.list: data_scope 过滤 / status filter / limit 截断 / 多重 filter
  - user.lookup: 4 selector（id/name/phone/email） / 0 selector 拒绝 /
    无匹配 / data_scope 越权拒绝
  - user.update: HITL 强制 / 字段白名单 / data_scope 越权拒绝 /
    无字段提供拒绝 / dry_run summary
"""

# ruff: noqa: ARG001, PLC0415  test 函数 ctx / kwargs 是与生产签名一致的占位

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.ai_tools import (
    _dry_run_user_update,
    user_list,
    user_lookup,
    user_update,
)
from app.modules.system.models.user import User

# ============ fixture ============


async def _add_user(
    db: AsyncSession,
    *,
    user_id: int,
    user_name: str,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    user_gender: str | None = None,
    status: str = "1",
) -> User:
    """建用户（绕过 ORM 关系，直接 insert）"""
    db.add(
        User(
            user_id=user_id,
            user_name=user_name,
            nickname=nickname or user_name,
            user_email=user_email,
            user_phone=user_phone,
            user_gender=user_gender,
            hashed_password="$2b$12$dummy",
            status=status,
        )
    )
    await db.flush()
    user = await db.get(User, user_id)
    assert user is not None
    return user


def _make_ctx(
    db: AsyncSession,
    *,
    visible_user_ids: set[int] | None = None,
    data_scope: DataScopeContext | None = None,
    tool_name: str = "user.list",
    required_perms: tuple[str, ...] = ("system:user:list",),
    allowed_filters: tuple[str, ...] = ("status", "user_gender"),
) -> AiToolContext:
    """构造 AiToolContext（参考 test_system_ai_tools.py 同款 fixture）"""
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
        name=tool_name,
        agent="user_mgmt",
        summary="x",
        required_perms=required_perms,
        risk="low",
        allowed_filters=allowed_filters,
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms=set(required_perms),
        db=db,
        data_scope=data_scope,
        trace_id="tr_test",
        tool_meta=meta,
    )


# ============ user.list ============


class TestUserList:
    async def test_list_returns_users_in_data_scope(
        self, db_session: AsyncSession
    ) -> None:
        """data_scope 内的 3 个用户 → list 返回 3 条"""
        await _add_user(db_session, user_id=2001, user_name="alice")
        await _add_user(db_session, user_id=2002, user_name="bob")
        await _add_user(db_session, user_id=2003, user_name="carol")
        # data_scope 外的用户（不应被列出）
        await _add_user(db_session, user_id=9001, user_name="outsider")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={2001, 2002, 2003})
        result = await user_list(ctx, filters=None, limit=10)

        assert result.data["total"] == 3
        assert result.data["limit"] == 10
        assert len(result.data["sample"]) == 3
        assert result.ui.view_type == "data_list"
        assert len(result.ui.view_data["rows"]) == 3
        user_names = {r["user_name"] for r in result.ui.view_data["rows"]}
        assert user_names == {"alice", "bob", "carol"}

    async def test_list_with_status_filter(self, db_session: AsyncSession) -> None:
        """status='1' filter：3 启用 + 1 禁用 → 返回 3"""
        await _add_user(db_session, user_id=2010, user_name="a1", status="1")
        await _add_user(db_session, user_id=2011, user_name="a2", status="1")
        await _add_user(db_session, user_id=2012, user_name="a3", status="1")
        await _add_user(db_session, user_id=2013, user_name="d1", status="2")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={2010, 2011, 2012, 2013})
        result = await user_list(ctx, filters={"status": "1"}, limit=20)

        assert result.data["total"] == 3
        statuses = {r["status"] for r in result.ui.view_data["rows"]}
        assert statuses == {"1"}

    async def test_list_limit_truncation(self, db_session: AsyncSession) -> None:
        """limit > 50 → 截断到 50"""
        for i in range(5):
            await _add_user(db_session, user_id=2020 + i, user_name=f"u{i}")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={2020, 2021, 2022, 2023, 2024})
        result = await user_list(ctx, filters=None, limit=200)

        assert result.data["limit"] == 50  # _LIST_MAX_LIMIT 截断
        assert result.data["total"] == 5  # total 反映真实总数

    async def test_list_limit_none_uses_default(self, db_session: AsyncSession) -> None:
        """limit=None → 默认 20"""
        await _add_user(db_session, user_id=2030, user_name="solo")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={2030})
        result = await user_list(ctx, filters=None, limit=None)

        assert result.data["limit"] == 20  # _LIST_DEFAULT_LIMIT


# ============ user.lookup ============


class TestUserLookup:
    async def test_lookup_by_user_id(self, db_session: AsyncSession) -> None:
        """user_id selector → 单条返回"""
        await _add_user(
            db_session,
            user_id=3001,
            user_name="david",
            user_email="david@example.com",
            user_phone="13800003001",
            user_gender="1",
        )
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={3001})
        result = await user_lookup(ctx, user_id=3001)

        assert result.data["user_name"] == "david"
        assert result.data["id"] == "3001"
        assert result.ui.view_type == "detail_card"
        assert result.ui.view_data["user_email"] == "david@example.com"
        assert result.ui.view_data["user_phone"] == "13800003001"

    async def test_lookup_by_user_name(self, db_session: AsyncSession) -> None:
        """user_name selector → 单条返回"""
        await _add_user(db_session, user_id=3002, user_name="emily")
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={3002})
        result = await user_lookup(ctx, user_name="emily")

        assert result.data["user_name"] == "emily"

    async def test_lookup_by_phone(self, db_session: AsyncSession) -> None:
        """phone selector → 单条返回"""
        await _add_user(
            db_session, user_id=3003, user_name="frank", user_phone="13900003003"
        )
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={3003})
        result = await user_lookup(ctx, phone="13900003003")

        assert result.data["user_name"] == "frank"

    async def test_lookup_by_email(self, db_session: AsyncSession) -> None:
        """email selector → 单条返回"""
        await _add_user(
            db_session,
            user_id=3004,
            user_name="grace",
            user_email="grace@example.com",
        )
        await db_session.flush()

        ctx = _make_ctx(db_session, visible_user_ids={3004})
        result = await user_lookup(ctx, email="grace@example.com")

        assert result.data["user_name"] == "grace"

    async def test_lookup_no_selector_raises(self, db_session: AsyncSession) -> None:
        """0 selector → BusinessRuleException AI_LOOKUP_NO_TARGET"""
        ctx = _make_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await user_lookup(ctx)
        assert exc_info.value.error_code == "AI_LOOKUP_NO_TARGET"

    async def test_lookup_no_match_raises(self, db_session: AsyncSession) -> None:
        """无匹配 → BusinessRuleException AI_LOOKUP_NO_MATCH"""
        ctx = _make_ctx(db_session, visible_user_ids={3010})
        with pytest.raises(BusinessRuleException) as exc_info:
            await user_lookup(ctx, user_name="nonexistent")
        assert exc_info.value.error_code == "AI_LOOKUP_NO_MATCH"

    async def test_lookup_ambiguous_phone_fails_closed(
        self, db_session: AsyncSession
    ) -> None:
        """A non-unique selector must never choose an arbitrary write target."""
        shared_phone = "13900003999"
        await _add_user(
            db_session,
            user_id=3031,
            user_name="lookup-ambiguous-a",
            user_phone=shared_phone,
        )
        await _add_user(
            db_session,
            user_id=3032,
            user_name="lookup-ambiguous-b",
            user_phone=shared_phone,
        )
        await db_session.flush()
        ctx = _make_ctx(db_session, visible_user_ids={3031, 3032})

        with pytest.raises(BusinessRuleException) as exc_info:
            await user_lookup(ctx, phone=shared_phone)

        assert exc_info.value.error_code == "AI_LOOKUP_AMBIGUOUS"

    async def test_lookup_data_scope_excludes_outsider(
        self, db_session: AsyncSession
    ) -> None:
        """data_scope 外用户 → AI_LOOKUP_NO_MATCH（即使存在）"""
        await _add_user(db_session, user_id=3050, user_name="outsider")
        await db_session.flush()

        # data_scope filters 不含 3050
        ctx = _make_ctx(db_session, visible_user_ids=set())  # 空 visible
        with pytest.raises(BusinessRuleException) as exc_info:
            await user_lookup(ctx, user_id=3050)
        assert exc_info.value.error_code == "AI_LOOKUP_NO_MATCH"


# ============ user.update ============


class TestUserUpdate:
    async def test_update_requires_at_least_one_field(
        self, db_session: AsyncSession
    ) -> None:
        """无字段提供 → AI_USER_UPDATE_NO_FIELDS"""
        await _add_user(db_session, user_id=4001, user_name="hank")
        await db_session.flush()

        ctx = _make_ctx(
            db_session,
            visible_user_ids={4001},
            tool_name="user.update",
            required_perms=("system:user:edit",),
        )
        with pytest.raises(BusinessRuleException) as exc_info:
            await user_update(ctx, user_id=4001)
        assert exc_info.value.error_code == "AI_USER_UPDATE_NO_FIELDS"

    async def test_update_nickname_success(self, db_session: AsyncSession) -> None:
        """更新 nickname → 用户对象被更新 + 返回 rows_affected"""
        await _add_user(db_session, user_id=4002, user_name="iris")
        await db_session.flush()

        ctx = _make_ctx(
            db_session,
            visible_user_ids={4002},
            tool_name="user.update",
            required_perms=("system:user:edit",),
        )
        result = await user_update(ctx, user_id=4002, nickname="iris-new")

        assert result.data == {"updated": 1, "userName": "iris"}
        assert result.ui.view_type == "rows_affected"
        assert result.ui.view_data["count"] == 1
        assert result.ui.view_data["ids"] == ["4002"]

        # 验证 DB 状态
        user = await db_session.get(User, 4002)
        assert user.nickname == "iris-new"

    async def test_update_multiple_fields(self, db_session: AsyncSession) -> None:
        """同时更新 nickname + status → audit.fields 含两个"""
        await _add_user(db_session, user_id=4003, user_name="jack", status="1")
        await db_session.flush()

        ctx = _make_ctx(
            db_session,
            visible_user_ids={4003},
            tool_name="user.update",
            required_perms=("system:user:edit",),
        )
        result = await user_update(ctx, user_id=4003, nickname="jack-new", status="2")

        assert set(result.ui.audit["fields"]) == {"nickname", "status"}

    async def test_update_data_scope_blocks_outsider(
        self, db_session: AsyncSession
    ) -> None:
        """data_scope 外 user_id → ensure_targets_in_scope 或 select(User) 返回 None

        本 fixture 用 `visible_user_ids=set()` → ctx.data_scope.filters 含
        `User.user_id.in_(set())`（空集），select 必返 None → NotFoundException。
        完整 AI_DATA_SCOPE_VIOLATION 路径在 test_authz_matrix.py 覆盖。
        """
        await _add_user(db_session, user_id=4050, user_name="outsider")
        await db_session.flush()

        ctx = _make_ctx(
            db_session,
            visible_user_ids=set(),
            tool_name="user.update",
            required_perms=("system:user:edit",),
        )
        from app.core.exceptions import (
            AuthorizationException,
            NotFoundException,
        )

        with pytest.raises((AuthorizationException, NotFoundException)):
            await user_update(ctx, user_id=4050, nickname="hacked")


class TestDryRunUserUpdate:
    async def test_dry_run_returns_examples(self, db_session: AsyncSession) -> None:
        """dry_run：列出 fields 变更清单"""
        await _add_user(
            db_session,
            user_id=4100,
            user_name="kate",
            nickname="kate-old",
            status="1",
        )
        await db_session.flush()

        ctx = _make_ctx(
            db_session,
            visible_user_ids={4100},
            tool_name="user.update",
            required_perms=("system:user:edit",),
        )
        result = await _dry_run_user_update(
            ctx, user_id=4100, nickname="kate-new", status="2"
        )

        assert result.ok is True
        assert result.count == 1
        assert "2 个字段" in result.reason
        assert any("nickname" in ex for ex in result.examples)
        assert any("status" in ex for ex in result.examples)
        assert result.confirmation_fields == [
            {
                "label": "user_id",
                "value": 4100,
                "display_value": "kate（4100）",
            },
            {"label": "nickname", "value": "kate-new"},
            {"label": "status", "value": "2"},
        ]

        fields = _build_direct_confirmation_fields(
            user_update.__ai_tool_meta__,
            {"user_id": 4100, "nickname": "kate-new", "status": "2"},
            DryRunSummary(
                summary=result.reason,
                affected_count=result.count,
                confirmation_fields=result.confirmation_fields,
            ),
        )
        assert fields == [
            {"label": "user_id", "value": "kate（4100）"},
            {"label": "nickname", "value": "kate-new"},
            {"label": "status", "value": "2"},
            {"label": "affectedCount", "value": 1, "tone": "warning"},
        ]

    async def test_dry_run_no_fields_returns_not_ok(
        self, db_session: AsyncSession
    ) -> None:
        """dry_run 无字段 → ok=False"""
        ctx = _make_ctx(
            db_session,
            tool_name="user.update",
            required_perms=("system:user:edit",),
        )
        result = await _dry_run_user_update(ctx, user_id=4200)
        assert result.ok is False
        assert result.count == 0

    async def test_dry_run_user_not_found(self, db_session: AsyncSession) -> None:
        """dry_run 用户不存在 → ok=False"""
        ctx = _make_ctx(
            db_session,
            visible_user_ids=set(),
            tool_name="user.update",
            required_perms=("system:user:edit",),
        )
        result = await _dry_run_user_update(ctx, user_id=9999, nickname="x")
        assert result.ok is False
        assert "不存在" in result.reason or "不在" in result.reason
