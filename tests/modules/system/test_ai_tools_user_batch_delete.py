"""user.batch_delete selectors 字段名 / data_scope 测试

防回归：User 模型字段是 user_phone（不是 phone），user_names/phones selectors
+ dry_run examples 都必须用对字段名，否则 dry_run 抛 AttributeError 被兜底
catch 成「预估失败（内部错误）」，用户抽屉显示 count=0。

bug 现场（2026-07-18 E2E）：发「删除用户 cs123」→ 抽屉弹 count=0 + 内部错误，
stderr 是 AttributeError: 'User' object has no attribute 'phone'。

Task 7（2026-07-28）：migrate 到 ToolResult.success + UIResult(rows_affected)。
LLM 只看 {"deleted": N}，user_ids 移到 ui.view_data.ids + ui.audit.affected_user_ids。
"""

# ruff: noqa: PLC0415

from unittest.mock import MagicMock

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.ai_tools import (
    _dry_run_user_batch_delete,
    _resolve_users,
    user_batch_delete,
)
from app.modules.system.models.user import User


def _make_ctx(
    db, *, accessible_user_scope: Select[tuple[int]] | None = None
) -> AiToolContext:
    """构造 super_admin 视角的 AiToolContext（filters 空，全可见）"""
    user = MagicMock()
    user.user_id = 1
    user.user_name = "admin"
    return AiToolContext(
        user=user,
        perms=set(),
        db=db,
        data_scope=DataScopeContext(
            accessible_dept_ids=None,
            accessible_user_scope=accessible_user_scope,
            filters=[],
        ),
        trace_id="test-trace",
        tool_meta=AiToolMeta(
            name="user.batch_delete",
            agent="user_mgmt",
            summary="test",
            required_perms=("system:user:delete",),
            risk="destructive",
        ),
    )


@pytest.mark.usefixtures("db_session")
class TestResolveUsersFieldNames:
    """字段名必须用 User.user_phone（不是 phone），否则 AttributeError"""

    async def test_phones_selector_uses_user_phone_not_phone(self, db_session) -> None:
        """phones selector 走 User.user_phone.in_(...)，不抛 AttributeError"""
        ctx = _make_ctx(db_session)
        # 字段不存在时 _resolve_users 内部 getattr(User, "phone") 会抛 AttributeError
        # 修复后应正常返回空 list（无匹配手机号）
        users = await _resolve_users(
            ctx, user_ids=None, user_names=None, phones=["13800000000"]
        )
        assert users == []

    async def test_user_names_selector_returns_match(self, db_session) -> None:
        """user_names selector 走 User.user_name.in_(...)，能查到 admin"""
        ctx = _make_ctx(db_session)
        users = await _resolve_users(
            ctx, user_ids=None, user_names=["admin"], phones=None
        )
        assert len(users) >= 1
        assert all(isinstance(u, User) for u in users)
        assert any(u.user_name == "admin" for u in users)

    async def test_no_selectors_raises(self, db_session) -> None:
        """三个 selector 全 None → BusinessRuleException（防 LLM 漏传参）"""
        from app.core.exceptions import BusinessRuleException

        ctx = _make_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await _resolve_users(ctx, user_ids=None, user_names=None, phones=None)
        assert exc_info.value.error_code == "AI_BATCH_DELETE_NO_TARGETS"


@pytest.mark.usefixtures("db_session")
class TestDryRunExamplesUseUserPhone:
    """dry_run examples 必须读 u.user_phone（不是 u.phone）"""

    async def test_dry_run_examples_with_user_phone(self, db_session) -> None:
        """dry_run 命中时 examples 用 u.user_phone，不抛 AttributeError"""
        ctx = _make_ctx(db_session)
        result = await _dry_run_user_batch_delete(
            ctx, user_ids=None, user_names=["admin"], phones=None
        )
        assert result.ok is True
        assert result.count >= 1
        # examples 应是非空 list，每条含 "phone:" 字面量（即使值是 "-"）
        assert result.examples is not None
        assert len(result.examples) >= 1
        assert "phone:" in result.examples[0]

    async def test_dry_run_no_match_returns_ok_false(self, db_session) -> None:
        """无匹配 → ok=False + count=0 + 中文 reason"""
        ctx = _make_ctx(db_session)
        result = await _dry_run_user_batch_delete(
            ctx, user_ids=None, user_names=None, phones=["13800000000"]
        )
        assert result.ok is False
        assert result.count == 0
        assert "未找到匹配用户" in (result.reason or "")


async def _add_user(
    db: AsyncSession,
    *,
    user_id: int,
    user_name: str,
) -> None:
    """建用户（绕过 ORM 关系，直接 insert）"""
    db.add(
        User(
            user_id=user_id,
            user_name=user_name,
            nickname=user_name,
            hashed_password="$2b$12$dummy",
            status="1",
        )
    )
    await db.flush()


@pytest.mark.usefixtures("db_session")
class TestUserBatchDeleteToolResult:
    """Task 7: user_batch_delete 迁移到 ToolResult.success + UIResult(rows_affected)"""

    async def test_returns_tool_result_with_rows_affected_view(
        self, db_session
    ) -> None:
        """LLM 只看 {"deleted": N}；user_ids 进 ui.view_data.ids + ui.audit.affected_user_ids"""
        await _add_user(db_session, user_id=1001, user_name="bd1")
        await _add_user(db_session, user_id=1002, user_name="bd2")
        await db_session.flush()

        ctx = _make_ctx(
            db_session,
            accessible_user_scope=None,
        )
        # data_scope.filters 限定为这两个 ID，避免误删 admin 等历史用户
        ctx.data_scope.filters.append(User.user_id.in_([1001, 1002]))

        result = await user_batch_delete(ctx, user_ids=[1001, 1002])

        # LLM 只看 deleted count（user_ids 不在 data 里）
        assert result.ok is True
        assert result.data == {"deleted": 2}
        # UI 层：rows_affected 视图
        assert result.ui is not None
        assert result.ui.view_type == "rows_affected"
        assert result.ui.view_data["count"] == 2
        assert result.ui.view_data["ids"] == ["1001", "1002"]
        # 审计字段：affected_user_ids 供后台审计页反查
        assert result.ui.audit["affected_user_ids"] == ["1001", "1002"]

    async def test_single_delete_returns_count_one(self, db_session) -> None:
        """单条删除 → count=1 + ids 长度 1"""
        await _add_user(db_session, user_id=1003, user_name="bd3")
        await db_session.flush()

        ctx = _make_ctx(db_session)
        ctx.data_scope.filters.append(User.user_id.in_([1003]))

        result = await user_batch_delete(ctx, user_ids=[1003])

        assert result.data == {"deleted": 1}
        assert result.ui.view_data["count"] == 1
        assert result.ui.view_data["ids"] == ["1003"]
        assert result.ui.audit["affected_user_ids"] == ["1003"]
