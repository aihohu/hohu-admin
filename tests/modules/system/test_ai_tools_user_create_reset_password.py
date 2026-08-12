"""AI user.create / user.reset_password 回归测试。

按 docs/specs/2026-08-11-ai-user-management-tools.md：敏感密码只由后端
私有配置生成，不进入 tool schema、ToolResult 或确认摘要。
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE, USER_ROLE_CODE
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.security import verify_password
from app.db.base import user_depts, user_roles
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.registry import ToolRegistry, compute_available_tools
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.ai_tools import (
    _dry_run_user_create,
    _dry_run_user_reset_password,
    user_create,
    user_dept_lookup,
    user_reset_password,
)
from app.modules.system.models.config import Config
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from scripts.seed_agent_prompts import (
    DEFAULT_PROMPTS,
    LEGACY_DEFAULT_PROMPTS,
    should_update_prompt,
)

DEFAULT_PASSWORD = "AiPolicy123"


async def _seed_default_password(db: AsyncSession) -> None:
    await db.execute(delete(Config).where(Config.config_key == "auth:default_password"))
    db.add(
        Config(
            config_name="用户默认密码",
            config_key="auth:default_password",
            config_value=DEFAULT_PASSWORD,
            config_group="auth",
            status=STATUS_ENABLED,
            is_public=False,
        )
    )
    await db.flush()


async def _seed_default_role(db: AsyncSession) -> Role:
    role = await db.scalar(select(Role).where(Role.role_code == USER_ROLE_CODE))
    if role is None:
        role = Role(
            role_name="AI 工具默认用户",
            role_code=USER_ROLE_CODE,
            role_desc="default user role",
            data_scope="5",
            status=STATUS_ENABLED,
        )
        db.add(role)
    else:
        role.status = STATUS_ENABLED
    await db.flush()
    return role


async def _add_dept(
    db: AsyncSession,
    dept_id: int,
    name: str,
    *,
    parent_id: int | None = None,
) -> Dept:
    dept = Dept(
        dept_id=dept_id,
        parent_id=parent_id,
        ancestors="0",
        dept_name=name,
        order_num=1,
        status=STATUS_ENABLED,
    )
    db.add(dept)
    await db.flush()
    return dept


async def _add_user(
    db: AsyncSession,
    *,
    user_id: int,
    user_name: str,
    password_hash: str = "$2b$12$dummy",
) -> User:
    user = User(
        user_id=user_id,
        user_name=user_name,
        nickname=user_name,
        hashed_password=password_hash,
        status=STATUS_ENABLED,
    )
    db.add(user)
    await db.flush()
    return user


def _make_ctx(
    db: AsyncSession,
    *,
    tool_name: str,
    permission: str,
    actor: User | None = None,
    visible_user_ids: set[int] | None = None,
    accessible_dept_ids: set[int] | None = None,
) -> AiToolContext:
    filters = []
    if visible_user_ids is not None:
        filters.append(User.user_id.in_(visible_user_ids))
    meta = AiToolMeta(
        name=tool_name,
        agent="user_mgmt",
        summary="test",
        required_perms=(permission,),
        risk="high",
    )
    return AiToolContext(
        user=actor or MagicMock(user_id=1, user_name="operator", roles=[]),
        perms={permission},
        db=db,
        data_scope=DataScopeContext(
            accessible_dept_ids=accessible_dept_ids,
            accessible_user_scope=None,
            filters=filters,
        ),
        trace_id="tr_user_management_tools",
        tool_meta=meta,
    )


class TestUserToolMetadata:
    @pytest.mark.parametrize(
        "tool",
        [user_create, user_reset_password],
    )
    def test_sensitive_password_is_backend_only(self, tool) -> None:
        meta = tool.__ai_tool_meta__
        signature = inspect.signature(tool)

        assert meta.risk == "high"
        assert meta.hitl_always is True
        assert meta.dry_run_supported is True
        assert "password" in meta.sensitive_input
        assert not {
            "password",
            "new_password",
            "hashed_password",
        } & set(signature.parameters)

    def test_tools_use_expected_permissions(self) -> None:
        assert user_create.__ai_tool_meta__.required_perms == ("system:user:add",)
        assert user_dept_lookup.__ai_tool_meta__.required_perms == ("system:user:add",)
        assert user_reset_password.__ai_tool_meta__.required_perms == (
            "system:user:reset-password",
        )

    def test_dept_lookup_is_readonly_and_owned_by_user_agent(self) -> None:
        meta = user_dept_lookup.__ai_tool_meta__
        assert meta.agent == "user_mgmt"
        assert meta.readonly is True
        assert meta.idempotent is True
        assert meta.risk == "low"

    def test_user_creator_can_see_lookup_and_create_together(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ToolRegistry()
        monkeypatch.setattr(ToolRegistry, "_instance", registry)
        registry.register(user_dept_lookup.__ai_tool_meta__, user_dept_lookup)
        registry.register(user_create.__ai_tool_meta__, user_create)

        visible = {
            tool.meta.name
            for tool in compute_available_tools({"system:user:add"}, "user_mgmt")
        }
        assert {"user.dept_lookup", "user.create"} <= visible


class TestUserDeptLookup:
    def test_user_agent_prompt_requires_lookup_before_create(self) -> None:
        prompt = DEFAULT_PROMPTS["user_mgmt"]
        assert "user.dept_lookup" in prompt
        assert "唯一命中" in prompt
        assert "不要要求用户输入部门 ID" in prompt

    async def test_exact_name_returns_only_visible_enabled_match(
        self, db_session: AsyncSession
    ) -> None:
        parent = await _add_dept(db_session, 81301, "解析测试集团")
        visible = await _add_dept(
            db_session,
            81302,
            "解析测试总部",
            parent_id=parent.dept_id,
        )
        await _add_dept(db_session, 81303, "解析测试总部")
        ctx = _make_ctx(
            db_session,
            tool_name="user.dept_lookup",
            permission="system:user:add",
            accessible_dept_ids={visible.dept_id},
        )

        result = await user_dept_lookup(ctx, dept_name=" 解析测试总部 ")

        assert result.data == {
            "query": "解析测试总部",
            "matchCount": 1,
            "matches": [
                {
                    "id": str(visible.dept_id),
                    "name": visible.dept_name,
                    "parentId": str(parent.dept_id),
                    "parentName": parent.dept_name,
                }
            ],
        }
        assert result.ui.view_type == "data_list"
        assert result.ui.view_data["rows"] == result.data["matches"]

    async def test_duplicate_visible_names_return_all_candidates(
        self, db_session: AsyncSession
    ) -> None:
        parent_a = await _add_dept(db_session, 81304, "解析测试华东")
        parent_b = await _add_dept(db_session, 81305, "解析测试华南")
        dept_a = await _add_dept(
            db_session, 81306, "解析测试销售部", parent_id=parent_a.dept_id
        )
        dept_b = await _add_dept(
            db_session, 81307, "解析测试销售部", parent_id=parent_b.dept_id
        )
        ctx = _make_ctx(
            db_session,
            tool_name="user.dept_lookup",
            permission="system:user:add",
            accessible_dept_ids={dept_a.dept_id, dept_b.dept_id},
        )

        result = await user_dept_lookup(ctx, dept_name="解析测试销售部")

        assert result.data["matchCount"] == 2
        assert {row["parentName"] for row in result.data["matches"]} == {
            parent_a.dept_name,
            parent_b.dept_name,
        }

    async def test_unknown_name_returns_zero_matches(
        self, db_session: AsyncSession
    ) -> None:
        ctx = _make_ctx(
            db_session,
            tool_name="user.dept_lookup",
            permission="system:user:add",
            accessible_dept_ids=set(),
        )

        result = await user_dept_lookup(ctx, dept_name="不存在的解析测试部门")

        assert result.data == {
            "query": "不存在的解析测试部门",
            "matchCount": 0,
            "matches": [],
        }


class TestUserCreate:
    def test_confirmation_uses_dept_name_without_changing_frozen_id(self) -> None:
        dept_id = 7455072815813758976
        frozen_args = {
            "user_name": "圣诞",
            "primary_dept_id": dept_id,
        }
        summary = DryRunSummary(
            summary="将创建用户圣诞",
            affected_count=1,
            confirmation_fields=[
                {
                    "label": "primary_dept_id",
                    "value": dept_id,
                    "display_value": f"总部（{dept_id}）",
                }
            ],
        )

        fields = _build_direct_confirmation_fields(
            user_create.__ai_tool_meta__, frozen_args, summary
        )

        assert fields == [
            {"label": "user_name", "value": "圣诞"},
            {"label": "primary_dept_id", "value": f"总部（{dept_id}）"},
            {"label": "affectedCount", "value": 1, "tone": "warning"},
        ]
        assert frozen_args["primary_dept_id"] == dept_id

    def test_confirmation_rejects_display_value_bound_to_another_id(self) -> None:
        dept_id = 7455072815813758976
        summary = DryRunSummary(
            summary="将创建用户圣诞",
            affected_count=1,
            confirmation_fields=[
                {
                    "label": "primary_dept_id",
                    "value": dept_id + 1,
                    "display_value": f"另一个部门（{dept_id + 1}）",
                }
            ],
        )

        with pytest.raises(ValueError, match="frozen argument"):
            _build_direct_confirmation_fields(
                user_create.__ai_tool_meta__,
                {"user_name": "圣诞", "primary_dept_id": dept_id},
                summary,
            )

    async def test_create_uses_default_password_role_and_primary_dept(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_default_password(db_session)
        await _seed_default_role(db_session)
        dept = await _add_dept(db_session, 81001, "AI 产品部")
        ctx = _make_ctx(
            db_session,
            tool_name="user.create",
            permission="system:user:add",
            accessible_dept_ids={dept.dept_id},
        )

        result = await user_create(
            ctx,
            user_name="aitooluser",
            primary_dept_id=dept.dept_id,
            nickname="AI 工具用户",
            user_email="ai-tool@example.com",
        )

        created = await db_session.scalar(
            select(User).where(User.user_name == "aitooluser")
        )
        assert created is not None
        assert verify_password(DEFAULT_PASSWORD, created.hashed_password)
        assert [role.role_code for role in created.roles] == [USER_ROLE_CODE]
        dept_link = (
            await db_session.execute(
                select(user_depts.c.dept_id, user_depts.c.is_primary).where(
                    user_depts.c.user_id == created.user_id
                )
            )
        ).one()
        assert dept_link == (dept.dept_id, "Y")
        assert result.data == {
            "created": 1,
            "userId": str(created.user_id),
            "userName": "aitooluser",
            "roleCode": USER_ROLE_CODE,
            "primaryDeptId": str(dept.dept_id),
            "passwordPolicy": "system_default",
        }
        assert result.ui.view_type == "detail_card"
        assert result.ui.view_data["title"] == "aitooluser"
        assert len(result.ui.view_data["fields"]) == 4
        assert DEFAULT_PASSWORD not in repr(result)
        assert "hashed_password" not in repr(result)

    async def test_create_rejects_dept_outside_scope(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_default_password(db_session)
        await _seed_default_role(db_session)
        dept = await _add_dept(db_session, 81002, "不可见部门")
        ctx = _make_ctx(
            db_session,
            tool_name="user.create",
            permission="system:user:add",
            accessible_dept_ids=set(),
        )

        with pytest.raises(Exception) as exc_info:
            await user_create(
                ctx,
                user_name="outscopeuser",
                primary_dept_id=dept.dept_id,
            )

        assert getattr(exc_info.value, "error_code", "") == ("AI_DATA_SCOPE_VIOLATION")

    async def test_create_fails_when_default_role_missing(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_default_password(db_session)
        role = await db_session.scalar(
            select(Role).where(Role.role_code == USER_ROLE_CODE)
        )
        if role is not None:
            role.status = "2"
            await db_session.flush()
        dept = await _add_dept(db_session, 81003, "默认角色缺失部门")
        ctx = _make_ctx(
            db_session,
            tool_name="user.create",
            permission="system:user:add",
            accessible_dept_ids={dept.dept_id},
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await user_create(
                ctx,
                user_name="noroleuser",
                primary_dept_id=dept.dept_id,
            )

        assert exc_info.value.error_code == "AI_USER_DEFAULT_ROLE_NOT_FOUND"

    async def test_create_rejects_weak_default_password_configuration(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_default_password(db_session)
        config = await db_session.scalar(
            select(Config).where(Config.config_key == "auth:default_password")
        )
        assert config is not None
        config.config_value = "weakpassword"
        await _seed_default_role(db_session)
        dept = await _add_dept(db_session, 81005, "弱密码策略部门")
        ctx = _make_ctx(
            db_session,
            tool_name="user.create",
            permission="system:user:add",
            accessible_dept_ids={dept.dept_id},
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await user_create(
                ctx,
                user_name="weakpolicyuser",
                primary_dept_id=dept.dept_id,
            )

        assert exc_info.value.error_code == "AI_USER_DEFAULT_PASSWORD_INVALID"

    async def test_dry_run_does_not_create_user(self, db_session: AsyncSession) -> None:
        await _seed_default_password(db_session)
        await _seed_default_role(db_session)
        dept = await _add_dept(db_session, 81004, "预检部门")
        ctx = _make_ctx(
            db_session,
            tool_name="user.create",
            permission="system:user:add",
            accessible_dept_ids={dept.dept_id},
        )

        result = await _dry_run_user_create(
            ctx,
            user_name="previewuser",
            primary_dept_id=dept.dept_id,
        )

        assert result.ok is True
        assert result.count == 1
        assert result.confirmation_fields == [
            {
                "label": "primary_dept_id",
                "value": dept.dept_id,
                "display_value": f"{dept.dept_name}（{dept.dept_id}）",
            }
        ]
        assert DEFAULT_PASSWORD not in repr(result)
        assert (
            await db_session.scalar(select(User).where(User.user_name == "previewuser"))
            is None
        )


class TestUserResetPassword:
    async def test_reset_uses_system_default_without_returning_secret(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_default_password(db_session)
        actor = await _add_user(db_session, user_id=82001, user_name="passwordoperator")
        target = await _add_user(db_session, user_id=82002, user_name="passwordtarget")
        ctx = _make_ctx(
            db_session,
            tool_name="user.reset_password",
            permission="system:user:reset-password",
            actor=actor,
            visible_user_ids={target.user_id},
        )

        result = await user_reset_password(ctx, user_id=target.user_id)

        assert verify_password(DEFAULT_PASSWORD, target.hashed_password)
        assert result.data == {
            "updated": 1,
            "userId": str(target.user_id),
            "userName": target.user_name,
            "passwordPolicy": "system_default",
        }
        assert result.ui.view_type == "rows_affected"
        assert DEFAULT_PASSWORD not in repr(result)

    async def test_reset_rejects_current_user(self, db_session: AsyncSession) -> None:
        await _seed_default_password(db_session)
        actor = await _add_user(db_session, user_id=82003, user_name="selfreset")
        ctx = _make_ctx(
            db_session,
            tool_name="user.reset_password",
            permission="system:user:reset-password",
            actor=actor,
            visible_user_ids={actor.user_id},
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await user_reset_password(ctx, user_id=actor.user_id)

        assert exc_info.value.error_code == "AI_USER_RESET_SELF_FORBIDDEN"

    async def test_non_super_admin_cannot_reset_system_admin(
        self, db_session: AsyncSession
    ) -> None:
        actor = await _add_user(
            db_session, user_id=82006, user_name="delegatedpasswordadmin"
        )
        target = await db_session.scalar(select(User).where(User.user_name == "admin"))
        if target is None:
            target = await _add_user(db_session, user_id=82007, user_name="admin")
        ctx = _make_ctx(
            db_session,
            tool_name="user.reset_password",
            permission="system:user:reset-password",
            actor=actor,
            visible_user_ids={target.user_id},
        )

        with pytest.raises(AuthorizationException) as exc_info:
            await user_reset_password(ctx, user_id=target.user_id)

        assert exc_info.value.error_code == "AI_SUPER_ADMIN_REQUIRED"

    async def test_non_super_admin_cannot_reset_other_super_admin(
        self, db_session: AsyncSession
    ) -> None:
        actor = await _add_user(
            db_session, user_id=82008, user_name="delegatedresetoperator"
        )
        target = await _add_user(
            db_session, user_id=82009, user_name="anotherrootoperator"
        )
        super_role = await db_session.scalar(
            select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
        )
        if super_role is None:
            super_role = Role(
                role_name="超级管理员",
                role_code=SUPER_ADMIN_ROLE_CODE,
                status=STATUS_ENABLED,
            )
            db_session.add(super_role)
            await db_session.flush()
        else:
            super_role.status = STATUS_ENABLED
        await db_session.execute(
            user_roles.insert().values(
                user_id=target.user_id,
                role_id=super_role.role_id,
            )
        )
        await db_session.flush()
        ctx = _make_ctx(
            db_session,
            tool_name="user.reset_password",
            permission="system:user:reset-password",
            actor=actor,
            visible_user_ids={target.user_id},
        )

        with pytest.raises(AuthorizationException) as exc_info:
            await user_reset_password(ctx, user_id=target.user_id)

        assert exc_info.value.error_code == "AI_SUPER_ADMIN_REQUIRED"

    async def test_dry_run_reports_target_without_password(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_default_password(db_session)
        actor = await _add_user(db_session, user_id=82004, user_name="dryrunoperator")
        target = await _add_user(db_session, user_id=82005, user_name="dryruntarget")
        ctx = _make_ctx(
            db_session,
            tool_name="user.reset_password",
            permission="system:user:reset-password",
            actor=actor,
            visible_user_ids={target.user_id},
        )

        result = await _dry_run_user_reset_password(ctx, user_id=target.user_id)

        assert result.ok is True
        assert result.count == 1
        assert target.user_name in result.reason
        assert result.confirmation_fields == [
            {
                "label": "user_id",
                "value": target.user_id,
                "display_value": f"{target.user_name}（{target.user_id}）",
            }
        ]
        assert DEFAULT_PASSWORD not in repr(result)


class TestUserManagementPromptUpgrade:
    def test_previous_builtin_prompt_is_upgraded_without_force(self) -> None:
        old_prompt = next(iter(LEGACY_DEFAULT_PROMPTS["user_mgmt"]))
        assert should_update_prompt("user_mgmt", old_prompt, force=False) is True

    def test_custom_prompt_is_preserved_without_force(self) -> None:
        assert (
            should_update_prompt(
                "user_mgmt",
                "这是部署方自定义的用户管理规则",
                force=False,
            )
            is False
        )
