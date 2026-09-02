"""Task 13 AI user role lookup and replacement contracts."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from tenant_helpers import bind_test_user, tenant_context

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
)
from app.core.exceptions import BusinessRuleException
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.agents.tools.pydantic_ai_wrapper import wrap_tool_for_pydantic_ai
from app.modules.ai.agents.tools.registry import RegisteredTool
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.system import ai_tools as system_ai_tools
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.tenant_association_writer import (
    replace_role_menus,
    replace_user_roles,
)
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)
from scripts.seed_agent_prompts import DEFAULT_PROMPTS

USER_EDIT_PERMISSION = "system:user:edit"


def _menu(permission: str) -> Menu:
    marker = next_id()
    return Menu(
        tenant_id=0,
        menu_id=marker,
        menu_name=f"task13-menu-{marker}",
        menu_type="F",
        permission=permission,
        status=STATUS_ENABLED,
    )


def _role(
    code: str,
    *,
    data_scope: str = DATA_SCOPE_SELF,
    menus: list[Menu] | None = None,
) -> Role:
    marker = next_id()
    role = Role(
        tenant_id=0,
        role_id=marker,
        role_name=f"task13-role-{marker}",
        role_code=code,
        data_scope=data_scope,
        status=STATUS_ENABLED,
    )
    role.menus = menus or []
    return role


def _user(name: str, roles: list[Role]) -> User:
    return User(
        tenant_id=0,
        user_id=next_id(),
        user_name=name,
        nickname=name,
        hashed_password="x",
        status=STATUS_ENABLED,
        roles=roles,
    )


def _tool_ctx(
    db: AsyncSession,
    *,
    actor: User,
    perms: set[str] | None = None,
) -> AiToolContext:
    tenant = bind_test_user(actor)
    return AiToolContext(
        user=actor,
        perms=(
            perms
            if perms is not None
            else {"system:user:list", USER_EDIT_PERMISSION, USER_ROLE_AUTH_PERMISSION}
        ),
        db=db,
        data_scope=DataScopeContext(
            tenant=tenant,
            accessible_dept_ids=None,
            accessible_user_scope=None,
            filters=[],
        ),
        trace_id="tr_task13_user_role_assignment",
        tool_meta=MagicMock(),
        tenant=tenant,
    )


async def _persist_graph(
    db: AsyncSession,
    *,
    users: list[User],
    roles: list[Role],
    menus: list[Menu],
    user_departments: dict[int, list[Dept]] | None = None,
) -> None:
    """Persist tenant-owned test graphs through explicit association writers."""
    user_role_links = {user.user_id: list(user.roles) for user in users}
    role_menu_links = {role.role_id: list(role.menus) for role in roles}
    department_links = user_departments or {}
    all_roles = {
        role.role_id: role
        for role in [*roles, *(role for user in users for role in user.roles)]
    }
    all_menus = {
        menu.menu_id: menu
        for menu in [
            *menus,
            *(menu for role in all_roles.values() for menu in role.menus),
        ]
    }
    all_depts = {
        dept.dept_id: dept
        for departments in department_links.values()
        for dept in departments
    }
    for user in users:
        set_committed_value(user, "roles", [])
        set_committed_value(user, "depts", [])
    for role in all_roles.values():
        set_committed_value(role, "menus", [])
    db.add_all([*all_menus.values(), *all_roles.values(), *all_depts.values(), *users])
    await db.flush()
    tenant = tenant_context(tenant_id=0, actor_user_id=users[0].user_id)
    for role in all_roles.values():
        await replace_role_menus(
            db,
            role,
            role_menu_links.get(role.role_id, []),
            tenant=tenant,
        )
    for user in users:
        await replace_user_roles(
            db,
            user,
            user_role_links[user.user_id],
            tenant=tenant,
        )
        departments = department_links.get(user.user_id, [])
        if departments:
            await db.execute(
                insert(user_depts),
                [
                    {
                        "tenant_id": 0,
                        "user_id": user.user_id,
                        "dept_id": dept.dept_id,
                        "is_primary": "Y" if index == 0 else "N",
                    }
                    for index, dept in enumerate(departments)
                ],
            )
        set_committed_value(user, "depts", departments)


def test_role_lookup_and_update_roles_metadata_contracts() -> None:
    lookup = getattr(system_ai_tools, "user_role_lookup", None)
    update = getattr(system_ai_tools, "user_update_roles", None)

    assert lookup is not None
    assert update is not None
    assert lookup.__ai_tool_meta__.required_perms == (USER_ROLE_AUTH_PERMISSION,)
    assert lookup.__ai_tool_meta__.readonly is True
    assert update.__ai_tool_meta__.required_perms == (
        USER_EDIT_PERMISSION,
        USER_ROLE_AUTH_PERMISSION,
    )
    assert update.__ai_tool_meta__.risk == "high"
    assert update.__ai_tool_meta__.hitl_always is True
    assert update.__ai_tool_meta__.dry_run_supported is True
    assert update.__ai_tool_meta__.args_summary_fields == ("user_id", "role_ids")
    assert ".commit(" not in inspect.getsource(update)


def test_update_roles_exposes_strict_complete_id_schema() -> None:
    tool = system_ai_tools.user_update_roles
    wrapped = wrap_tool_for_pydantic_ai(
        RegisteredTool(meta=tool.__ai_tool_meta__, fn=tool)
    )

    role_ids = wrapped.function_schema.json_schema["properties"]["role_ids"]

    assert role_ids["minItems"] == 1
    assert role_ids["items"]["type"] == "integer"
    assert role_ids["items"]["exclusiveMinimum"] == 0


def test_user_agent_prompt_requires_complete_role_replacement() -> None:
    prompt = DEFAULT_PROMPTS["user_mgmt"]

    assert "user.role_lookup(query=" in prompt
    assert "user.update_roles" in prompt
    assert "roleAssignmentsComplete=true" in prompt
    assert "唯一命中 → 使用该 roleId" in prompt
    assert "零命中 → 请用户检查角色编码或名称" in prompt
    assert "多命中 → 展示 roleCode/roleName 并请用户消歧" in prompt
    assert "完整 role_ids" in prompt
    assert "禁止只提交增量" in prompt


async def test_role_lookup_returns_only_assignable_minimal_candidates(
    db_session: AsyncSession,
) -> None:
    delegated_permission = _menu(f"task13:delegated:{next_id()}")
    outside_permission = _menu(f"task13:outside:{next_id()}")
    actor_role = _role(
        f"R_TASK13_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ROLE_AUTH_PERMISSION), delegated_permission],
    )
    assignable = _role(
        f"R_TASK13_MATCH_{next_id()}",
        menus=[delegated_permission],
    )
    blocked = _role(
        f"R_TASK13_MATCH_{next_id()}",
        menus=[outside_permission],
    )
    actor = _user(f"task13-lookup-actor-{next_id()}", [actor_role])
    await _persist_graph(
        db_session,
        users=[actor],
        roles=[actor_role, assignable, blocked],
        menus=[delegated_permission, outside_permission],
    )
    ctx = _tool_ctx(db_session, actor=actor)

    result = await system_ai_tools.user_role_lookup(
        ctx,
        query="R_TASK13_MATCH_",
        limit=20,
    )

    assert result.data == {
        "query": "R_TASK13_MATCH_",
        "matchCount": 1,
        "matches": [
            {
                "roleId": str(assignable.role_id),
                "roleCode": assignable.role_code,
                "roleName": assignable.role_name,
                "dataScope": assignable.data_scope,
            }
        ],
    }
    assert result.projection.subject_refs == (
        {"type": "delegable_role", "id": str(assignable.role_id)},
    )


async def test_role_lookup_projection_freezes_every_match_beyond_the_row_limit(
    db_session: AsyncSession,
) -> None:
    actor = _user(f"task13-actor-{next_id()}", [])
    ctx = _tool_ctx(db_session, actor=actor)
    first = SimpleNamespace(
        role_id=901,
        role_code="R_FIRST",
        role_name="First",
        data_scope=DATA_SCOPE_SELF,
    )
    page = SimpleNamespace(
        match_count=2,
        matched_role_ids=(901, 902),
        roles=(first,),
    )

    with patch.object(
        user_role_assignment_service,
        "lookup_assignable_roles",
        AsyncMock(return_value=page),
    ):
        result = await system_ai_tools.user_role_lookup(
            ctx,
            query="R_",
            limit=1,
        )

    assert result.data["matchCount"] == 2
    assert len(result.data["matches"]) == 1
    assert result.projection.subject_refs == (
        {"type": "delegable_role", "id": "901"},
        {"type": "delegable_role", "id": "902"},
    )


async def test_role_lookup_returns_an_explicit_zero_match_result(
    db_session: AsyncSession,
) -> None:
    actor = _user(f"task13-actor-{next_id()}", [])
    ctx = _tool_ctx(db_session, actor=actor)
    page = SimpleNamespace(match_count=0, matched_role_ids=(), roles=())

    with patch.object(
        user_role_assignment_service,
        "lookup_assignable_roles",
        AsyncMock(return_value=page),
    ):
        result = await system_ai_tools.user_role_lookup(
            ctx,
            query="missing-role",
            limit=20,
        )

    assert result.data == {
        "query": "missing-role",
        "matchCount": 0,
        "matches": [],
    }
    assert result.projection.subject_refs == ()


@pytest.mark.parametrize(
    ("query", "limit", "error_code"),
    [
        ("", 20, "AI_USER_ROLE_QUERY_REQUIRED"),
        ("role", 0, "AI_USER_ROLE_LOOKUP_LIMIT_INVALID"),
        ("role", 21, "AI_USER_ROLE_LOOKUP_LIMIT_INVALID"),
    ],
)
async def test_role_lookup_rejects_invalid_query_or_limit(
    db_session: AsyncSession,
    query: str,
    limit: int,
    error_code: str,
) -> None:
    actor = _user(f"task13-actor-{next_id()}", [])
    ctx = _tool_ctx(db_session, actor=actor)

    with pytest.raises(BusinessRuleException) as exc_info:
        await system_ai_tools.user_role_lookup(ctx, query=query, limit=limit)

    assert exc_info.value.error_code == error_code


async def test_user_lookup_returns_roles_only_when_complete_and_assignable(
    db_session: AsyncSession,
) -> None:
    delegated_permission = _menu(f"task13:current:{next_id()}")
    outside_permission = _menu(f"task13:current-outside:{next_id()}")
    actor_role = _role(
        f"R_TASK13_CURRENT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ROLE_AUTH_PERMISSION), delegated_permission],
    )
    assignable = _role(
        f"R_TASK13_CURRENT_OK_{next_id()}",
        menus=[delegated_permission],
    )
    blocked = _role(
        f"R_TASK13_CURRENT_NO_{next_id()}",
        menus=[outside_permission],
    )
    visible_target = _user(f"task13-current-visible-{next_id()}", [assignable])
    blocked_target = _user(f"task13-current-blocked-{next_id()}", [blocked])
    actor = _user(f"task13-current-actor-{next_id()}", [actor_role])
    await _persist_graph(
        db_session,
        users=[visible_target, blocked_target, actor],
        roles=[actor_role, assignable, blocked],
        menus=[delegated_permission, outside_permission],
    )
    ctx = _tool_ctx(db_session, actor=actor)

    visible = await system_ai_tools.user_lookup(
        ctx,
        user_id=visible_target.user_id,
    )
    blocked_result = await system_ai_tools.user_lookup(
        ctx,
        user_id=blocked_target.user_id,
    )

    assert visible.data["roleAssignmentsComplete"] is True
    assert visible.data["roleAssignments"] == [
        {
            "roleId": str(assignable.role_id),
            "roleCode": assignable.role_code,
            "roleName": assignable.role_name,
            "dataScope": assignable.data_scope,
            "status": assignable.status,
        }
    ]
    assert {tuple(ref.values()) for ref in visible.projection.subject_refs} == {
        ("user", str(visible_target.user_id)),
        ("complete_user_role_assignment", str(visible_target.user_id)),
        ("delegable_role", str(assignable.role_id)),
    }
    assert blocked_result.data["roleAssignmentsComplete"] is False
    assert blocked_result.data["roleAssignments"] == []
    assert {tuple(ref.values()) for ref in blocked_result.projection.subject_refs} == {
        ("user", str(blocked_target.user_id)),
        ("user_role_assignment_access", str(blocked_target.user_id)),
    }


async def test_user_lookup_without_role_auth_keeps_legacy_shape(
    db_session: AsyncSession,
) -> None:
    target = _user(f"task13-legacy-lookup-{next_id()}", [])
    actor = _user(f"task13-legacy-actor-{next_id()}", [])
    db_session.add(target)
    await db_session.flush()
    ctx = _tool_ctx(
        db_session,
        actor=actor,
        perms={"system:user:list"},
    )

    with patch.object(
        user_role_assignment_service,
        "get_complete_assignable_roles",
        AsyncMock(),
    ) as get_roles:
        result = await system_ai_tools.user_lookup(ctx, user_id=target.user_id)

    assert "roleAssignmentsComplete" not in result.data
    assert "roleAssignments" not in result.data
    get_roles.assert_not_awaited()


async def test_user_lookup_hides_template_allowed_roles_when_live_scope_exceeds_actor(
    db_session: AsyncSession,
) -> None:
    delegated_permission = _menu(f"task13:live-scope:{next_id()}")
    actor_role = _role(
        f"R_TASK13_LIVE_SCOPE_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_DEPT,
        menus=[_menu(USER_ROLE_AUTH_PERMISSION), delegated_permission],
    )
    target_role = _role(
        f"R_TASK13_LIVE_SCOPE_TARGET_{next_id()}",
        data_scope=DATA_SCOPE_DEPT,
        menus=[delegated_permission],
    )
    actor_dept = Dept(
        tenant_id=0,
        dept_id=next_id(),
        dept_name=f"task13-live-actor-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    outside_dept = Dept(
        tenant_id=0,
        dept_id=next_id(),
        dept_name=f"task13-live-outside-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    actor = _user(f"task13-live-actor-{next_id()}", [actor_role])
    target = _user(f"task13-live-target-{next_id()}", [target_role])
    await _persist_graph(
        db_session,
        users=[actor, target],
        roles=[actor_role, target_role],
        menus=[delegated_permission],
        user_departments={
            actor.user_id: [actor_dept],
            target.user_id: [actor_dept, outside_dept],
        },
    )
    ctx = _tool_ctx(db_session, actor=actor)

    result = await system_ai_tools.user_lookup(ctx, user_id=target.user_id)

    assert result.data["roleAssignmentsComplete"] is False
    assert result.data["roleAssignments"] == []


@pytest.mark.parametrize(
    ("role_ids", "error_code"),
    [
        ([], "USER_ROLE_NOT_AVAILABLE"),
        ([True], "USER_ROLE_NOT_AVAILABLE"),
        ([0], "USER_ROLE_NOT_AVAILABLE"),
        ([901, 901], "USER_ROLE_SET_DUPLICATE"),
    ],
)
async def test_update_roles_dry_run_rejects_invalid_complete_sets(
    role_ids: list[int],
    error_code: str,
) -> None:
    ctx = MagicMock(
        user=MagicMock(user_id=6001),
        tenant=tenant_context(tenant_id=0, actor_user_id=6001),
    )

    with patch.object(
        user_role_assignment_service,
        "preview_roles",
        AsyncMock(),
    ) as preview_roles:
        with pytest.raises(BusinessRuleException) as exc_info:
            await system_ai_tools._dry_run_user_update_roles(
                ctx,
                user_id=7001,
                role_ids=role_ids,
            )

    assert exc_info.value.error_code == error_code
    preview_roles.assert_not_awaited()


async def test_update_roles_dry_run_freezes_sorted_ids_and_snapshot() -> None:
    preview = SimpleNamespace(
        user_id=7001,
        user_name="task13-target",
        old_role_ids=(801, 802),
        new_role_ids=(901, 902),
        old_display=("Old A", "Old B"),
        new_display=("New A", "New B"),
        snapshot={"version": "task13-snapshot"},
    )
    ctx = MagicMock(
        user=MagicMock(user_id=6001),
        tenant=tenant_context(tenant_id=0, actor_user_id=6001),
    )

    with patch.object(
        user_role_assignment_service,
        "preview_roles",
        AsyncMock(return_value=preview),
        create=True,
    ):
        result = await system_ai_tools._dry_run_user_update_roles(
            ctx,
            user_id=7001,
            role_ids=[902, 901],
        )

    assert result.ok is True
    assert result.execution_args == {"user_id": 7001, "role_ids": [901, 902]}
    assert result.business_snapshot == preview.snapshot
    assert result.confirmation_fields == [
        {
            "label": "user_id",
            "value": 7001,
            "display_value": "task13-target",
        },
        {
            "label": "role_ids",
            "value": [902, 901],
            "display_value": ("Old A; Old B → New A; New B"),
        },
    ]


async def test_update_roles_executes_shared_policy_with_approved_snapshot(
    db_session: AsyncSession,
) -> None:
    actor = _user(f"task13-actor-{next_id()}", [])
    target = _user(f"task13-execute-{next_id()}", [])
    db_session.add(target)
    await db_session.flush()
    ctx = _tool_ctx(db_session, actor=actor)
    ctx.approved_business_snapshot = {
        "roleFacts": [
            {"roleId": "801", "roleCode": "R_OLD", "roleName": "Old"},
            {"roleId": "901", "roleCode": "R_NEW", "roleName": "New"},
        ]
    }
    replacement = SimpleNamespace(old_role_ids=(801,), new_role_ids=(901,))

    with patch.object(
        user_role_assignment_service,
        "replace_roles",
        AsyncMock(return_value=replacement),
    ) as replace_roles:
        result = await system_ai_tools.user_update_roles(
            ctx,
            user_id=target.user_id,
            role_ids=[901],
        )

    replace_roles.assert_awaited_once_with(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[901],
        expected_snapshot=ctx.approved_business_snapshot,
        tenant=ctx.tenant,
    )
    assert result.data == {
        "updated": 1,
        "userName": target.user_name,
        "previousRoles": "Old",
        "newRoles": "New",
    }
    assert result.projection.subject_refs == (
        {"type": "user", "id": str(target.user_id)},
        {"type": "complete_user_role_assignment", "id": str(target.user_id)},
        {"type": "delegable_role", "id": "801"},
        {"type": "delegable_role", "id": "901"},
    )


def test_update_roles_confirmation_binds_complete_collection() -> None:
    role_ids = [901, 902]
    fields = _build_direct_confirmation_fields(
        system_ai_tools.user_update_roles.__ai_tool_meta__,
        {"user_id": 7001, "role_ids": role_ids},
        DryRunSummary(
            summary="ignored",
            affected_count=1,
            confirmation_fields=[
                {
                    "label": "role_ids",
                    "value": role_ids,
                    "display_value": "Old → New A; New B",
                }
            ],
        ),
    )

    assert fields == [
        {"label": "user_id", "value": 7001},
        {"label": "role_ids", "value": "Old → New A; New B"},
        {"label": "affectedCount", "value": 1, "tone": "warning"},
    ]


async def test_update_roles_prepared_snapshot_rejects_business_drift(
    db_session: AsyncSession,
) -> None:
    frozen_args = {"user_id": 7001, "role_ids": [901]}
    snapshot = {
        "tool": "user.update_roles",
        "argsHash": canonical_payload_hash(frozen_args),
        "dryRun": None,
        "business": {"version": "approved"},
    }
    action = SimpleNamespace(
        tenant_id=0,
        execute_tool_name="user.update_roles",
        frozen_args=frozen_args,
        args_hash=canonical_payload_hash(frozen_args),
        snapshot=snapshot,
        snapshot_hash=canonical_payload_hash(snapshot),
        user_id=6001,
    )
    live_preview = SimpleNamespace(snapshot={"version": "changed"})

    with patch.object(
        user_role_assignment_service,
        "preview_roles",
        AsyncMock(return_value=live_preview),
        create=True,
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await prepared_action_service.validate_snapshot(
                db_session,
                action,
                tenant=tenant_context(tenant_id=0, actor_user_id=6001),
            )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"


def test_update_roles_pending_projection_freezes_old_and_new_targets() -> None:
    refs = prepared_action_service._build_subject_refs(
        execute_tool_name="user.update_roles",
        frozen_args={"user_id": 7001, "role_ids": [802, 803]},
        snapshot={"business": {"oldRoleIds": ["801", "802"]}},
        subject_ref=None,
        projection_kind=None,
    )

    assert refs == [
        {"type": "user", "id": "7001"},
        {"type": "complete_user_role_assignment", "id": "7001"},
        {"type": "delegable_role", "id": "801"},
        {"type": "delegable_role", "id": "802"},
        {"type": "delegable_role", "id": "803"},
    ]
