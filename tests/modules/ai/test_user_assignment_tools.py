"""Task 12 AI user department lookup and replacement contracts."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession
from tenant_helpers import bind_test_user, tenant_context

from app.constants import (
    IS_PRIMARY_NO,
    IS_PRIMARY_YES,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.ai.agents.gateway import atomic_execution
from app.modules.ai.agents.gateway import executor as gateway_executor
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.agents.tools.pydantic_ai_wrapper import wrap_tool_for_pydantic_ai
from app.modules.ai.agents.tools.registry import RegisteredTool
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.ai.seed_prompts import DEFAULT_PROMPTS
from app.modules.ai.service.atomic_completion_service import ClaimBinding
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.system import ai_tools as system_ai_tools
from app.modules.system.ai_tools import user_assignment as user_assignment_tools
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)


def _tool_ctx(
    db: AsyncSession,
    *,
    accessible_dept_ids: set[int] | None,
    perms: set[str] | None = None,
) -> AiToolContext:
    actor = User(
        tenant_id=0,
        user_id=next_id(),
        user_name=f"task12-actor-{next_id()}",
        nickname="Task 12 actor",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    tenant = bind_test_user(actor)
    return AiToolContext(
        user=actor,
        perms=(
            perms if perms is not None else {"system:user:edit", "system:dept:list"}
        ),
        db=db,
        data_scope=DataScopeContext(
            tenant=tenant,
            accessible_dept_ids=accessible_dept_ids,
            accessible_user_scope=None,
            filters=[],
        ),
        trace_id="tr_task12_user_assignment",
        tool_meta=system_ai_tools.user_dept_lookup.__ai_tool_meta__,
        tenant=tenant,
    )


def _dept(
    name: str,
    *,
    parent_id: int | None = None,
    ancestors: str = "0",
    status: str = STATUS_ENABLED,
) -> Dept:
    return Dept(
        tenant_id=0,
        dept_id=next_id(),
        parent_id=parent_id,
        ancestors=ancestors,
        dept_name=name,
        order_num=0,
        status=status,
    )


def test_dept_lookup_uses_department_permission_and_query_contract() -> None:
    meta = system_ai_tools.user_dept_lookup.__ai_tool_meta__

    assert meta.required_perms == ("system:dept:list",)
    assert {"ctx", "query", "limit"} <= set(
        system_ai_tools.user_dept_lookup.__annotations__
    )


async def test_dept_lookup_builds_paths_only_from_visible_enabled_nodes(
    db_session: AsyncSession,
) -> None:
    hidden_root = _dept(f"task12-hidden-{next_id()}")
    visible_parent = _dept(
        f"task12-visible-{next_id()}",
        parent_id=hidden_root.dept_id,
        ancestors=f"0,{hidden_root.dept_id}",
    )
    visible_leaf = _dept(
        f"task12-leaf-{next_id()}",
        parent_id=visible_parent.dept_id,
        ancestors=f"0,{hidden_root.dept_id},{visible_parent.dept_id}",
    )
    disabled_leaf = _dept(
        visible_leaf.dept_name,
        parent_id=visible_parent.dept_id,
        ancestors=f"0,{hidden_root.dept_id},{visible_parent.dept_id}",
        status=STATUS_DISABLED,
    )
    db_session.add_all([hidden_root, visible_parent, visible_leaf, disabled_leaf])
    await db_session.flush()
    ctx = _tool_ctx(
        db_session,
        accessible_dept_ids={
            visible_parent.dept_id,
            visible_leaf.dept_id,
            disabled_leaf.dept_id,
        },
    )

    result = await system_ai_tools.user_dept_lookup(
        ctx,
        query=f"{visible_parent.dept_name} / {visible_leaf.dept_name}",
        limit=20,
    )

    assert result.data == {
        "query": f"{visible_parent.dept_name} / {visible_leaf.dept_name}",
        "matchCount": 1,
        "matches": [
            {
                "deptId": str(visible_leaf.dept_id),
                "deptName": visible_leaf.dept_name,
                "path": f"{visible_parent.dept_name} / {visible_leaf.dept_name}",
            }
        ],
    }
    assert hidden_root.dept_name not in result.data["matches"][0]["path"]


async def test_dept_lookup_projection_freezes_every_count_contributor(
    db_session: AsyncSession,
) -> None:
    shared_name = f"task12-count-{next_id()}"
    departments = [_dept(shared_name) for _index in range(3)]
    db_session.add_all(departments)
    await db_session.flush()
    ctx = _tool_ctx(
        db_session,
        accessible_dept_ids={dept.dept_id for dept in departments},
    )

    result = await system_ai_tools.user_dept_lookup(
        ctx,
        query=shared_name,
        limit=1,
    )

    assert result.data["matchCount"] == 3
    assert len(result.data["matches"]) == 1
    assert result.projection.subject_refs == tuple(
        {"type": "dept", "id": str(dept_id)}
        for dept_id in sorted(dept.dept_id for dept in departments)
    )


@pytest.mark.parametrize("limit", [0, 21])
async def test_dept_lookup_rejects_out_of_contract_limit(
    db_session: AsyncSession,
    limit: int,
) -> None:
    ctx = _tool_ctx(db_session, accessible_dept_ids=set())

    with pytest.raises(BusinessRuleException) as exc_info:
        await system_ai_tools.user_dept_lookup(ctx, query="department", limit=limit)

    assert exc_info.value.error_code == "AI_USER_DEPT_LOOKUP_LIMIT_INVALID"


@pytest.mark.parametrize("query", ["", " / ", "Parent / ", "/ Child"])
async def test_dept_lookup_rejects_empty_path_segments(
    db_session: AsyncSession,
    query: str,
) -> None:
    ctx = _tool_ctx(db_session, accessible_dept_ids=set())

    with pytest.raises(BusinessRuleException) as exc_info:
        await system_ai_tools.user_dept_lookup(ctx, query=query)

    assert exc_info.value.error_code == "AI_USER_DEPT_QUERY_REQUIRED"


def test_update_dept_declares_complete_set_hitl_contract() -> None:
    tool = getattr(system_ai_tools, "user_update_dept", None)

    assert tool is not None
    meta = tool.__ai_tool_meta__
    assert meta.required_perms == ("system:user:edit", "system:dept:list")
    assert meta.risk == "high"
    assert meta.hitl_always is True
    assert meta.dry_run_supported is True
    assert meta.args_summary_fields == ("user_id", "dept_assignments")
    assert ".commit(" not in inspect.getsource(tool)


def test_update_dept_exposes_strict_assignment_schema_to_the_model() -> None:
    tool = system_ai_tools.user_update_dept
    wrapped = wrap_tool_for_pydantic_ai(
        RegisteredTool(meta=tool.__ai_tool_meta__, fn=tool)
    )

    schema = wrapped.function_schema.json_schema
    assignment_items = schema["properties"]["dept_assignments"]["items"]
    assignment_schema = schema["$defs"][assignment_items["$ref"].split("/")[-1]]

    assert assignment_schema["additionalProperties"] is False
    assert assignment_schema["required"] == ["dept_id", "is_primary"]
    assert assignment_schema["properties"]["dept_id"]["type"] == "integer"
    assert assignment_schema["properties"]["dept_id"]["exclusiveMinimum"] == 0
    assert assignment_schema["properties"]["is_primary"]["type"] == "boolean"


def test_user_agent_prompt_requires_complete_department_replacement() -> None:
    prompt = DEFAULT_PROMPTS["user_mgmt"]

    assert "user.dept_lookup(query=" in prompt
    assert "user.update_dept" in prompt
    assert "完整 dept_assignments" in prompt
    assert "禁止只提交增量" in prompt
    assert "departmentAssignmentsComplete=true" in prompt
    assert '"dept_id"' in prompt
    assert '"is_primary"' in prompt


async def test_user_lookup_returns_only_a_complete_visible_department_set(
    db_session: AsyncSession,
) -> None:
    target = User(
        tenant_id=0,
        user_id=next_id(),
        user_name=f"task12-lookup-{next_id()}",
        nickname="Task 12 Lookup",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    primary = _dept(f"task12-primary-{next_id()}")
    secondary = _dept(f"task12-secondary-{next_id()}")
    db_session.add_all([target, primary, secondary])
    await db_session.flush()
    await db_session.execute(
        insert(user_depts),
        [
            {
                "tenant_id": 0,
                "user_id": target.user_id,
                "dept_id": primary.dept_id,
                "is_primary": IS_PRIMARY_YES,
            },
            {
                "tenant_id": 0,
                "user_id": target.user_id,
                "dept_id": secondary.dept_id,
                "is_primary": IS_PRIMARY_NO,
            },
        ],
    )
    full_ctx = _tool_ctx(
        db_session,
        accessible_dept_ids={primary.dept_id, secondary.dept_id},
        perms={"system:user:list", "system:dept:list"},
    )

    full_result = await system_ai_tools.user_lookup(
        full_ctx,
        user_id=target.user_id,
    )

    assert full_result.data["departmentAssignmentsComplete"] is True
    assert full_result.data["departmentAssignments"] == [
        {
            "deptId": str(primary.dept_id),
            "deptName": primary.dept_name,
            "isPrimary": True,
            "status": STATUS_ENABLED,
        },
        {
            "deptId": str(secondary.dept_id),
            "deptName": secondary.dept_name,
            "isPrimary": False,
            "status": STATUS_ENABLED,
        },
    ]
    assert full_result.projection.subject_refs == (
        {"type": "user", "id": str(target.user_id)},
        {"type": "dept", "id": str(primary.dept_id)},
        {"type": "dept", "id": str(secondary.dept_id)},
    )

    partial_ctx = _tool_ctx(
        db_session,
        accessible_dept_ids={primary.dept_id},
        perms={"system:user:list", "system:dept:list"},
    )
    partial_result = await system_ai_tools.user_lookup(
        partial_ctx,
        user_id=target.user_id,
    )

    assert partial_result.data["departmentAssignmentsComplete"] is False
    assert partial_result.data["departmentAssignments"] == []
    assert partial_result.projection.subject_refs == (
        {"type": "user", "id": str(target.user_id)},
    )


async def test_user_lookup_preserves_legacy_data_without_department_permission(
    db_session: AsyncSession,
) -> None:
    target = User(
        tenant_id=0,
        user_id=next_id(),
        user_name=f"task12-lookup-legacy-{next_id()}",
        nickname="Task 12 Lookup Legacy",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    db_session.add(target)
    await db_session.flush()
    ctx = _tool_ctx(
        db_session,
        accessible_dept_ids=None,
        perms={"system:user:list"},
    )

    result = await system_ai_tools.user_lookup(ctx, user_id=target.user_id)

    assert "departmentAssignmentsComplete" not in result.data
    assert "departmentAssignments" not in result.data


def test_dept_lookup_statement_applies_limit_before_result_materialization() -> None:
    stmt = system_ai_tools._build_scoped_dept_lookup_stmt(
        tenant_id=0,
        accessible_dept_ids={701, 702},
        normalized_query="Region / Sales",
        limit=7,
    )

    assert stmt._limit_clause is not None
    assert stmt._limit_clause.value == 7


async def test_update_dept_dry_run_freezes_normalized_execution_and_snapshot() -> None:
    dry_run = getattr(system_ai_tools, "_dry_run_user_update_dept", None)
    assert dry_run is not None
    original_assignments = [
        {"dept_id": 902, "is_primary": False},
        {"dept_id": 901, "is_primary": True},
    ]
    preview = SimpleNamespace(
        user_id=7001,
        user_name="task12-target",
        old_assignments=((8001, True),),
        new_assignments=((901, True), (902, False)),
        old_display=("★ Old",),
        new_display=("★ New A", "New B"),
        snapshot={"version": "task12-snapshot"},
    )
    ctx = MagicMock(
        user=MagicMock(user_id=6001),
        tenant=tenant_context(tenant_id=0, actor_user_id=6001),
    )

    with patch.object(
        user_department_assignment_service,
        "preview_departments",
        AsyncMock(return_value=preview),
        create=True,
    ):
        result = await dry_run(
            ctx,
            user_id=7001,
            dept_assignments=original_assignments,
        )

    assert result.ok is True
    assert result.count == 1
    assert result.execution_args == {
        "user_id": 7001,
        "dept_assignments": [
            {"dept_id": 901, "is_primary": True},
            {"dept_id": 902, "is_primary": False},
        ],
    }
    assert result.business_snapshot == preview.snapshot
    assert result.confirmation_fields == [
        {
            "label": "user_id",
            "value": 7001,
            "display_value": "task12-target",
        },
        {
            "label": "dept_assignments",
            "value": [
                {"dept_id": 901, "is_primary": True},
                {"dept_id": 902, "is_primary": False},
            ],
            "display_value": "★ Old → ★ New A; New B",
        },
    ]


async def test_update_dept_executes_shared_policy_with_approved_snapshot(
    db_session: AsyncSession,
) -> None:
    tool = system_ai_tools.user_update_dept
    target = User(
        tenant_id=0,
        user_id=next_id(),
        user_name=f"task12-execute-{next_id()}",
        nickname="Task 12 Execute",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    db_session.add(target)
    await db_session.flush()
    old_dept_id = next_id()
    new_dept_id = next_id()
    approved_snapshot = {
        "departmentFacts": [
            {"deptId": str(old_dept_id), "deptName": "Old"},
            {"deptId": str(new_dept_id), "deptName": "New"},
        ]
    }
    ctx = _tool_ctx(db_session, accessible_dept_ids={new_dept_id})
    ctx.approved_business_snapshot = approved_snapshot
    replacement = SimpleNamespace(
        old_assignments=((old_dept_id, True),),
        new_assignments=((new_dept_id, True),),
    )

    with patch.object(
        user_department_assignment_service,
        "replace_departments",
        AsyncMock(return_value=replacement),
    ) as replace_departments:
        result = await tool(
            ctx,
            user_id=target.user_id,
            dept_assignments=[{"dept_id": new_dept_id, "is_primary": True}],
        )

    replace_departments.assert_awaited_once_with(
        db_session,
        actor_user_id=ctx.user.user_id,
        target_user_id=target.user_id,
        dept_assignments=[(new_dept_id, True)],
        expected_snapshot=approved_snapshot,
        tenant=ctx.tenant,
    )
    assert result.data == {
        "updated": 1,
        "userName": target.user_name,
        "previousDepartments": "★ Old",
        "newDepartments": "★ New",
    }
    assert result.projection.subject_refs == (
        {"type": "user", "id": str(target.user_id)},
        {"type": "dept", "id": str(old_dept_id)},
        {"type": "dept", "id": str(new_dept_id)},
    )
    assert result.ui is not None
    assert result.ui.view_data["fields"] == [
        {
            "label": "page.ai.chat.previousDepartments",
            "value": "★ Old",
        },
        {
            "label": "page.ai.chat.newDepartments",
            "value": "★ New",
        },
    ]


async def test_update_dept_maps_post_approval_scope_drift_to_snapshot_stale(
    db_session: AsyncSession,
) -> None:
    ctx = _tool_ctx(db_session, accessible_dept_ids={901})
    ctx.approved_business_snapshot = {"version": "approved"}

    with (
        patch.object(
            user_assignment_tools,
            "ensure_targets_in_scope",
            AsyncMock(
                side_effect=AuthorizationException(
                    "scope changed",
                    error_code="AI_DATA_SCOPE_VIOLATION",
                )
            ),
        ),
        patch.object(
            user_department_assignment_service,
            "replace_departments",
            AsyncMock(),
        ) as replace_departments,
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await system_ai_tools.user_update_dept(
                ctx,
                user_id=7001,
                dept_assignments=[{"dept_id": 901, "is_primary": True}],
            )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    replace_departments.assert_not_awaited()


def test_update_dept_confirmation_binds_complete_collection() -> None:
    tool = getattr(system_ai_tools, "user_update_dept", None)
    assert tool is not None
    assignments = [{"dept_id": 901, "is_primary": True}]
    summary = DryRunSummary(
        summary="ignored backend summary",
        affected_count=1,
        confirmation_fields=[
            {
                "label": "dept_assignments",
                "value": assignments,
                "display_value": "★ Old (8001) → ★ New (901)",
            }
        ],
    )

    fields = _build_direct_confirmation_fields(
        tool.__ai_tool_meta__,
        {"user_id": 7001, "dept_assignments": assignments},
        summary,
    )

    assert fields == [
        {"label": "user_id", "value": 7001},
        {"label": "dept_assignments", "value": "★ Old (8001) → ★ New (901)"},
        {"label": "affectedCount", "value": 1, "tone": "warning"},
    ]


async def test_approved_execution_injects_server_business_snapshot() -> None:
    expected = ToolResult.success(data={"updated": 1})
    registered = RegisteredTool(
        meta=user_assignment_tools.user_update_dept.__ai_tool_meta__,
        fn=AsyncMock(return_value=expected),
    )
    deps = SimpleNamespace(
        tenant=tenant_context(actor_user_id=6001),
        user=SimpleNamespace(user_id=6001),
        data_scope_hash=None,
    )
    frozen_args = {"user_id": 7001, "dept_assignments": []}
    snapshot = {"business": {"version": "approved"}}
    action = SimpleNamespace(
        tenant_id=0,
        action_id=9001,
        confirmation_id="assignment-snapshot-claim",
        execute_tool_call_id="assignment-snapshot-tool-call",
        execute_tool_name="user.update_dept",
        user_id=6001,
        conversation_id=9002,
        source_user_message_id=9003,
        trace_id="tr_assignment_claim",
        agent_code="user_mgmt",
        status="running",
        row_version=3,
        execution_owner="v0162-db:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        snapshot=snapshot,
        frozen_args=frozen_args,
        args_hash=canonical_payload_hash(frozen_args),
        snapshot_hash=canonical_payload_hash(snapshot),
    )
    claim = ClaimBinding.from_action(action, log_id=9004)
    tool_db = AsyncMock(spec=AsyncSession)
    tool_db.begin = AsyncMock()
    tool_context = SimpleNamespace(db=tool_db)

    async def immediate_timeout(awaitable, *, tenant):
        assert tenant == deps.tenant
        return await awaitable

    with (
        patch.object(
            gateway_executor,
            "validate_prepared_execution",
            return_value=registered,
        ),
        patch.object(atomic_execution, "AsyncSessionLocal", return_value=tool_db),
        patch.object(
            atomic_execution.atomic_completion_service,
            "lock_claim",
            AsyncMock(return_value=SimpleNamespace(action=action)),
        ),
        patch.object(
            atomic_execution.prepared_action_service, "validate_snapshot", AsyncMock()
        ),
        patch.object(
            atomic_execution.prepared_action_service, "validate_data_scope_snapshot"
        ),
        patch.object(atomic_execution, "ensure_current_write_authority", AsyncMock()),
        patch.object(atomic_execution, "with_l3_timeout", immediate_timeout),
        patch.object(
            atomic_execution, "build_tool_context", return_value=tool_context
        ) as build_context,
        patch.object(
            atomic_execution.atomic_completion_service, "complete_success", AsyncMock()
        ) as complete,
        patch.object(atomic_execution, "clear_failures", AsyncMock()),
        patch.object(gateway_executor, "_invoke_tool_fn", AsyncMock()) as legacy_invoke,
    ):
        result = await gateway_executor.execute_approved_prepared_action(
            claim,
            deps,
        )

    assert result is expected
    build_context.assert_called_once_with(
        deps,
        tool_db,
        registered.meta,
        approved_business_snapshot={"version": "approved"},
    )
    registered.fn.assert_awaited_once_with(tool_context, **frozen_args)
    complete.assert_awaited_once()
    assert complete.await_args.args[0] is tool_db
    assert complete.await_args.kwargs["result"] is expected
    legacy_invoke.assert_not_awaited()


async def test_write_execution_without_server_claim_is_rejected() -> None:
    registered = RegisteredTool(
        meta=user_assignment_tools.user_update_dept.__ai_tool_meta__,
        fn=AsyncMock(),
    )
    with (
        patch.object(
            gateway_executor, "validate_prepared_execution", return_value=registered
        ),
        patch.object(gateway_executor, "_invoke_tool_fn", AsyncMock()) as legacy_invoke,
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await gateway_executor.execute_approved_prepared_action(
                SimpleNamespace(tenant_id=0), MagicMock()
            )
    assert exc_info.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"
    registered.fn.assert_not_awaited()
    legacy_invoke.assert_not_awaited()


async def test_update_dept_prepared_snapshot_rejects_business_drift(
    db_session: AsyncSession,
) -> None:
    frozen_args = {
        "user_id": 7001,
        "dept_assignments": [{"dept_id": 901, "is_primary": True}],
    }
    snapshot = {
        "tool": "user.update_dept",
        "argsHash": canonical_payload_hash(frozen_args),
        "dryRun": None,
        "business": {"version": "approved"},
    }
    action = SimpleNamespace(
        tenant_id=0,
        execute_tool_name="user.update_dept",
        frozen_args=frozen_args,
        args_hash=canonical_payload_hash(frozen_args),
        snapshot=snapshot,
        snapshot_hash=canonical_payload_hash(snapshot),
        user_id=6001,
    )
    live_preview = SimpleNamespace(snapshot={"version": "changed"})

    with patch.object(
        user_department_assignment_service,
        "preview_departments",
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


def test_update_dept_pending_projection_freezes_old_and_new_targets() -> None:
    snapshot = {
        "business": {
            "oldAssignments": [
                {"deptId": "801", "isPrimary": True},
                {"deptId": "802", "isPrimary": False},
            ]
        }
    }

    refs = prepared_action_service._build_subject_refs(
        execute_tool_name="user.update_dept",
        frozen_args={
            "user_id": 7001,
            "dept_assignments": [
                {"dept_id": 802, "is_primary": False},
                {"dept_id": 803, "is_primary": True},
            ],
        },
        snapshot=snapshot,
        subject_ref=None,
        projection_kind=None,
    )

    assert refs == [
        {"type": "user", "id": "7001"},
        {"type": "dept", "id": "801"},
        {"type": "dept", "id": "802"},
        {"type": "dept", "id": "803"},
    ]
