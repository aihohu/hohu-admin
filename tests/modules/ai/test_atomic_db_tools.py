"""Real DB14 handlers share the business/action/audit transaction.

user.create has its deeper real fault matrix in test_atomic_prepared_completion.
Every remaining tool executes its real dry run, confirm and domain handler here.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.security import create_access_token
from app.core.tenant import TenantContext
from app.db.base import role_depts, role_menus, user_depts, user_roles
from app.db.session import engine
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.hitl.constants import AiOperationStatus
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.core.context import build_tool_context
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
)
from app.modules.ai.service.authorization_execution_candidate import (
    ai_execution_candidate,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.auth.service import get_current_user, load_live_user_authority
from app.modules.job import task_registry
from app.modules.job.models.job import SysJob
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal

TOOLS = (
    "job.update_cron",
    "dept.create",
    "dept.update",
    "dept.move",
    "role.create",
    "role.update",
    "role.update_menus",
    "role.update_agents",
    "user.update_dept",
    "user.update_roles",
    "user.reset_password",
    "user.batch_delete",
    "user.update",
)


@dataclass
class ToolCase:
    tool: str
    prefix: str
    tenant: TenantContext
    target_user_id: int
    target_conversation_id: int
    conversation_id: int
    provider_id: int
    action_id: int
    log_id: int
    confirmation_id: str
    args: dict[str, Any]
    previous_job_enabled: bool | None = None
    calls: list[tuple[int, int]] = field(default_factory=list)
    completion_transactions: list[tuple[int, int]] = field(default_factory=list)
    handler_states: list[dict[str, Any]] = field(default_factory=list)


async def _business_state(db: AsyncSession, case: ToolCase) -> dict[str, Any]:
    """Observe exact fixture-owned business facts, excluding incidental timestamps."""
    role_ids = select(Role.role_id).where(
        Role.tenant_id == 0, Role.role_code.startswith(case.prefix)
    )
    queries = {
        "users": select(
            User.user_id,
            User.nickname,
            User.status,
            User.hashed_password,
            User.auth_version,
        ).where(User.tenant_id == 0, User.user_id == case.target_user_id),
        "departments": select(
            Dept.dept_id, Dept.parent_id, Dept.ancestors, Dept.dept_name, Dept.status
        )
        .where(Dept.tenant_id == 0, Dept.dept_name.startswith(case.prefix))
        .order_by(Dept.dept_id),
        "roles": select(
            Role.role_id, Role.role_name, Role.role_desc, Role.data_scope, Role.status
        )
        .where(Role.tenant_id == 0, Role.role_code.startswith(case.prefix))
        .order_by(Role.role_id),
        "role_departments": select(role_depts.c.role_id, role_depts.c.dept_id)
        .where(role_depts.c.tenant_id == 0, role_depts.c.role_id.in_(role_ids))
        .order_by(role_depts.c.role_id, role_depts.c.dept_id),
        "role_menus": select(role_menus.c.role_id, role_menus.c.menu_id)
        .where(role_menus.c.tenant_id == 0, role_menus.c.role_id.in_(role_ids))
        .order_by(role_menus.c.role_id, role_menus.c.menu_id),
        "role_agents": select(
            RoleAiAgent.role_id, RoleAiAgent.agent_id, RoleAiAgent.enabled
        )
        .where(RoleAiAgent.tenant_id == 0, RoleAiAgent.role_id.in_(role_ids))
        .order_by(RoleAiAgent.role_id, RoleAiAgent.agent_id),
        "user_departments": select(user_depts.c.dept_id, user_depts.c.is_primary)
        .where(user_depts.c.tenant_id == 0, user_depts.c.user_id == case.target_user_id)
        .order_by(user_depts.c.dept_id),
        "user_roles": select(user_roles.c.role_id)
        .where(user_roles.c.tenant_id == 0, user_roles.c.user_id == case.target_user_id)
        .order_by(user_roles.c.role_id),
        "jobs": select(SysJob.job_id, SysJob.cron_expression, SysJob.status).where(
            SysJob.tenant_id == 0, SysJob.job_key == case.prefix
        ),
        "target_conversation": select(AiConversation.conversation_id).where(
            AiConversation.tenant_id == 0,
            AiConversation.conversation_id == case.target_conversation_id,
        ),
        "target_messages": select(AiMessage.message_id)
        .where(
            AiMessage.tenant_id == 0,
            AiMessage.conversation_id == case.target_conversation_id,
        )
        .order_by(AiMessage.message_id),
    }
    result = {
        name: [tuple(row) for row in (await db.execute(query)).all()]
        for name, query in queries.items()
    }

    result["users"] = [
        (*row[:3], sha256(row[3].encode()).hexdigest(), *row[4:])
        for row in result["users"]
    ]
    return result


async def authenticated_actor(db, tenant):
    version = await db.scalar(
        select(User.auth_version).where(
            User.tenant_id == tenant.tenant_id, User.user_id == tenant.actor_user_id
        )
    )
    return await get_current_user(
        create_access_token(
            subject=str(tenant.actor_user_id),
            tenant_id=tenant.tenant_id,
            tenant_version=tenant.tenant_version,
            user_version=version,
        ),
        db,
    )


def _arguments(
    tool, prefix, root, other, child, target, old_role, new_role, menu, agent, job
):
    return {
        "job.update_cron": {"job_id": job.job_id, "cron_expression": "0 4 * * *"},
        "dept.create": {"parent_id": root.dept_id, "dept_name": f"{prefix}-created"},
        "dept.update": {"dept_id": child.dept_id, "dept_name": f"{prefix}-renamed"},
        "dept.move": {"dept_id": child.dept_id, "new_parent_id": other.dept_id},
        "role.create": {
            "role_name": f"{prefix}-created",
            "role_code": f"{prefix}_CREATED",
            "data_scope": "CUSTOM",
            "status": "1",
            "dept_ids": [child.dept_id],
        },
        "role.update": {
            "role_id": old_role.role_id,
            "role_desc": "atomic role update",
            "data_scope": "CUSTOM",
            "dept_ids": [other.dept_id],
        },
        "role.update_menus": {"role_id": old_role.role_id, "menu_ids": [menu.menu_id]},
        "role.update_agents": {
            "role_id": old_role.role_id,
            "agent_ids": [agent.agent_id],
        },
        "user.update_dept": {
            "user_id": target.user_id,
            "dept_assignments": [{"dept_id": other.dept_id, "is_primary": True}],
        },
        "user.update_roles": {
            "user_id": target.user_id,
            "role_ids": [new_role.role_id],
        },
        "user.reset_password": {"user_id": target.user_id},
        "user.batch_delete": {"user_ids": [target.user_id]},
        "user.update": {"user_id": target.user_id, "nickname": "atomic updated"},
    }[tool]


@pytest.fixture
async def db_tool_case(request, db_session, monkeypatch) -> AsyncIterator[ToolCase]:
    assert settings.ENV == "test"
    assert db_session.bind is not None
    tool = request.param
    prefix = f"mx{uuid4().hex[:12]}"
    trace_id, confirmation_id, tool_call_id = (
        f"{kind}_{prefix}" for kind in ("tr", "cid", "tc")
    )
    monkeypatch.setattr(confirm_module, "redis_client", redis_module.redis_client)
    task = AsyncMock()
    monkeypatch.setitem(task_registry._task_functions, prefix, task)
    monkeypatch.setitem(
        task_registry._task_scopes, prefix, task_registry.TaskScope.TENANT
    )
    load_builtin_tools()
    registered = ToolRegistry.get().find(tool)
    assert registered is not None
    async with AsyncSessionLocal() as db, db.begin():
        await ToolRegistry.get().validate_on_startup(db)
        assert registered.dry_run_fn is not None
        super_role_id = await db.scalar(
            select(Role.role_id).where(Role.tenant_id == 0, Role.role_code == "R_SUPER")
        )
        assert super_role_id is not None
        root = Dept(tenant_id=0, dept_name=f"{prefix}-root", ancestors="0", status="1")
        other = Dept(
            tenant_id=0, dept_name=f"{prefix}-other", ancestors="0", status="1"
        )
        old_role = Role(
            tenant_id=0,
            role_name=f"{prefix}-old",
            role_code=f"{prefix}_OLD",
            data_scope="5",
            status="1",
        )
        new_role = Role(
            tenant_id=0,
            role_name=f"{prefix}-new",
            role_code=f"{prefix}_NEW",
            data_scope="5",
            status="1",
        )
        target = User(
            tenant_id=0,
            user_name=f"{prefix}target",
            nickname="atomic before",
            hashed_password="unusable-test-hash",
            status="1",
        )
        actor_user = User(
            tenant_id=0,
            user_name=f"{prefix}actor",
            nickname="matrix actor",
            hashed_password="unusable-test-hash",
            status="1",
        )
        grant_role = Role(
            tenant_id=0,
            role_name=f"{prefix}-grant",
            role_code=f"{prefix}_GRANT",
            data_scope="5",
            status="1",
        )
        provider = AiProvider(
            provider_code=prefix,
            name="No-network matrix fixture",
            api_key="unusable-test-key",
            base_url="https://invalid.example.invalid",
            is_enabled=True,
        )
        job = SysJob(
            tenant_id=0,
            job_name=prefix,
            job_key=prefix,
            cron_expression="0 3 * * *",
            status="2",
        )
        db.add_all(
            [
                root,
                other,
                old_role,
                new_role,
                grant_role,
                target,
                actor_user,
                provider,
                job,
            ]
        )
        await db.flush()
        actor_id = actor_user.user_id
        await db.execute(
            insert(user_roles),
            [
                {"tenant_id": 0, "user_id": actor_id, "role_id": super_role_id},
                {"tenant_id": 0, "user_id": actor_id, "role_id": grant_role.role_id},
            ],
        )
        menu_ids = list(
            await db.scalars(select(Menu.menu_id).where(Menu.tenant_id == 0))
        )
        await db.execute(
            insert(role_menus),
            [
                {"tenant_id": 0, "role_id": grant_role.role_id, "menu_id": menu_id}
                for menu_id in menu_ids
            ],
        )
        previous_job_enabled = None
        if tool == "job.update_cron":
            job_agent = await db.scalar(
                select(AiAgent).where(AiAgent.code == "job_mgmt")
            )
            assert job_agent is not None
            previous_job_enabled = job_agent.enabled
            job_agent.enabled = True
            db.add(
                RoleAiAgent(
                    tenant_id=0,
                    role_id=grant_role.role_id,
                    agent_id=job_agent.agent_id,
                    enabled=True,
                )
            )
            await db.flush()
        tenant = TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=actor_id,
            tenant_version=1,
            source="access_token",
        )
        actor = await load_live_user_authority(db, tenant=tenant)
        child = Dept(
            tenant_id=0,
            dept_name=f"{prefix}-child",
            parent_id=root.dept_id,
            ancestors=f"0,{root.dept_id}",
            status="1",
        )
        db.add(child)
        await db.flush()
        leaf = Dept(
            tenant_id=0,
            dept_name=f"{prefix}-leaf",
            parent_id=child.dept_id,
            ancestors=f"0,{root.dept_id},{child.dept_id}",
            status="1",
        )
        db.add(leaf)
        await db.execute(
            insert(user_roles).values(
                tenant_id=0, user_id=target.user_id, role_id=old_role.role_id
            )
        )
        await db.execute(
            insert(user_depts).values(
                tenant_id=0,
                user_id=target.user_id,
                dept_id=child.dept_id,
                is_primary="Y",
            )
        )
        menu = await db.scalar(
            select(Menu).where(
                Menu.tenant_id == 0, Menu.permission == "system:user:list"
            )
        )
        agent = await db.scalar(select(AiAgent).where(AiAgent.code == "user_mgmt"))
        assert menu is not None and agent is not None
        conversation = AiConversation(
            tenant_id=0,
            user_id=actor_id,
            title=prefix,
            agent_code=registered.meta.agent,
            trace_id=trace_id,
        )
        target_conversation = AiConversation(
            tenant_id=0,
            user_id=target.user_id,
            title=prefix,
            agent_code="user_mgmt",
            trace_id=f"target_{prefix}",
        )
        model = AiModel(
            provider_id=provider.provider_id,
            name=prefix,
            capabilities={"text": True, "tool_calling": True},
            is_enabled=True,
        )
        db.add_all([conversation, target_conversation, model])
        await db.flush()
        db.add(
            TenantAiModelPolicy(
                tenant_id=0, model_id=model.model_id, enabled=True, is_default=False
            )
        )
        source = AiMessage(
            tenant_id=0,
            conversation_id=conversation.conversation_id,
            role="user",
            content="Execute the fixture-owned operation",
            trace_id=trace_id,
            is_active=True,
        )
        target_message = AiMessage(
            tenant_id=0,
            conversation_id=target_conversation.conversation_id,
            role="user",
            content="Fixture-owned deletion dependency",
            trace_id=f"target_{prefix}",
            is_active=True,
        )
        db.add_all([source, target_message])
        await db.flush()
        deps = await chat_service.build_chat_deps(
            db,
            actor,
            agent_code=registered.meta.agent,
            trace_id=trace_id,
            conversation_id=conversation.conversation_id,
        )
        original_args = _arguments(
            tool,
            prefix,
            root,
            other,
            child,
            target,
            old_role,
            new_role,
            menu,
            agent,
            job,
        )
        dry_run = await registered.dry_run_fn(
            build_tool_context(deps, db, registered.meta), **original_args
        )
        assert dry_run.ok, dry_run.reason
        args = (
            dry_run.execution_args
            if dry_run.execution_args is not None
            else original_args
        )
        summary = DryRunSummary(
            summary=registered.meta.summary,
            affected_count=dry_run.count,
            confirmation_fields=dry_run.confirmation_fields,
            execution_args=dry_run.execution_args,
            business_snapshot=dry_run.business_snapshot,
        )
        snapshot = {"tool": tool, "argsHash": compute_args_hash(args), "dryRun": None}
        if dry_run.business_snapshot is not None:
            snapshot["business"] = dry_run.business_snapshot
        action = await prepared_action_service.create_pending(
            db,
            confirmation_id=confirmation_id,
            prepare_tool_call_id=None,
            prepare_tool_name=None,
            execute_tool_call_id=tool_call_id,
            execute_tool_name=tool,
            frozen_args=args,
            snapshot=snapshot,
            snapshot_hash="",
            subject_ref=None,
            presentation={
                "title": tool,
                "fields": _build_direct_confirmation_fields(
                    registered.meta, args, summary
                ),
            },
            interaction_flow="direct",
            requested_outcome="direct",
            user_id=actor_id,
            tenant=tenant,
            conversation_id=conversation.conversation_id,
            source_user_message_id=source.message_id,
            trace_id=trace_id,
            agent_code=registered.meta.agent,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            resolved_model_id=model.model_id,
            resolved_provider_id=provider.provider_id,
            projection_kind=registered.meta.projection_kind,
            data_scope_hash=deps.data_scope_hash,
            require_live_source=True,
        )
        log_id = await operation_log_service.start_operation(
            db,
            trace_id=trace_id,
            conversation_id=conversation.conversation_id,
            tenant=tenant,
            source_user_message_id=source.message_id,
            readonly_snapshot=False,
            agent_code=registered.meta.agent,
            user_id=actor_id,
            tool_name=tool,
            tool_call_id=tool_call_id,
            args_hash=action.args_hash,
            args_summary=tool,
            risk_level=registered.meta.risk,
            execution_mode="hitl",
            status=AiOperationStatus.PENDING_CONFIRMATION,
            confirmation_id=confirmation_id,
        )
        case = ToolCase(
            tool,
            prefix,
            tenant,
            target.user_id,
            target_conversation.conversation_id,
            conversation.conversation_id,
            provider.provider_id,
            action.action_id,
            log_id,
            confirmation_id,
            args,
            previous_job_enabled,
        )
    try:
        yield case
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            for model_type, condition in (
                (AiPreparedAction, AiPreparedAction.action_id == case.action_id),
                (AiOperationLog, AiOperationLog.log_id == case.log_id),
                (
                    AiConversation,
                    AiConversation.conversation_id.in_(
                        [case.conversation_id, case.target_conversation_id]
                    ),
                ),
                (
                    User,
                    User.user_id.in_([case.target_user_id, case.tenant.actor_user_id]),
                ),
                (Role, Role.role_code.startswith(prefix)),
                (SysJob, SysJob.job_key == prefix),
            ):
                await db.execute(
                    delete(model_type).where(model_type.tenant_id == 0, condition)
                )
            department_ids = list(
                await db.scalars(
                    select(Dept.dept_id)
                    .where(Dept.tenant_id == 0, Dept.dept_name.startswith(prefix))
                    .order_by(func.length(Dept.ancestors).desc())
                )
            )
            for department_id in department_ids:
                await db.execute(
                    delete(Dept).where(
                        Dept.tenant_id == 0, Dept.dept_id == department_id
                    )
                )
            await db.execute(
                delete(AiProvider).where(AiProvider.provider_id == case.provider_id)
            )
            if case.previous_job_enabled is not None:
                await db.execute(
                    update(AiAgent)
                    .where(AiAgent.code == "job_mgmt")
                    .values(enabled=case.previous_job_enabled)
                )
        await redis_module.redis_client.delete(
            f"ai:tenant:0:failures:{tenant.actor_user_id}:{tool}:{compute_args_hash(case.args)}"
        )
        task.assert_not_awaited()


def test_real_handler_matrix_covers_the_db_tool_inventory():
    assert set(TOOLS) | {"user.create"} == DB_TOOLS


@pytest.mark.parametrize(
    "selector", [{"user_names": ["alice"]}, {"phones": ["123"]}, {"unknown": None}]
)
async def test_planned_delete_does_not_reresolve_or_ignore_unknown_selectors(selector):
    with pytest.raises(BusinessRuleException) as caught:
        await system_ai_tool_adapter.prepare_input(
            None,
            tool="user.batch_delete",
            args={"user_ids": [101], **selector},
            snapshot={},
            tenant=TenantContext(0, "default", 101, 1, "access_token"),
            actor_user_version=1,
        )
    assert caught.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"


@pytest.mark.parametrize("db_tool_case", TOOLS, indirect=True)
@pytest.mark.parametrize(
    "reject_audit", [False, True], ids=["commit", "audit-rollback"]
)
async def test_real_db_tool_and_required_completion_share_one_transaction(
    db_tool_case, monkeypatch, reject_audit
):
    case = db_tool_case
    original = system_ai_tool_adapter.apply
    async with AsyncSessionLocal() as db:
        before = await _business_state(db, case)

    async def counted(db, tx, command, work):
        case.calls.append(
            (
                id(db),
                id(db.sync_session.get_transaction()),
            )
        )
        result = await original(db, tx, command, work)
        assert result.ok, result.error_code
        return result

    monkeypatch.setattr(system_ai_tool_adapter, "apply", counted)
    original_complete = ai_execution_candidate._complete

    async def observed_complete(db, *args, **kwargs):
        # Observe after the real Gateway's flush, without adding a test-only
        # flush that could hide missing business persistence in the adapter.
        case.completion_transactions.append(
            (
                id(db),
                id(db.sync_session.get_transaction()),
            )
        )
        case.handler_states.append(await _business_state(db, case))
        return await original_complete(db, *args, **kwargs)

    monkeypatch.setattr(ai_execution_candidate, "_complete", observed_complete)
    hits = []

    def reject_success_audit(_conn, _cursor, _sql, _params, context, _executemany):
        statement = getattr(context.compiled, "statement", None)
        if (
            not getattr(statement, "is_update", False)
            or statement.table.name != "ai_operation_log"
        ):
            return
        for parameters in context.compiled_parameters:
            if (
                parameters.get("status") == "success"
                and case.log_id in parameters.values()
            ):
                hits.append(True)
                raise RuntimeError("required matrix success audit SQL rejected")

    if reject_audit:
        event.listen(engine.sync_engine, "before_cursor_execute", reject_success_audit)
    try:
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(db, case.tenant)
            response = await confirm_module.confirm_tool(
                ConfirmRequest(confirmation_id=case.confirmation_id, action="approve"),
                db=db,
                current_user=actor,
            )
    finally:
        if reject_audit:
            event.remove(
                engine.sync_engine, "before_cursor_execute", reject_success_audit
            )

    assert len(case.calls) == len(case.handler_states) == 1
    assert case.calls == case.completion_transactions
    assert case.handler_states[0] != before, (
        "The real handler must change business facts"
    )
    async with AsyncSessionLocal() as db:
        after = await _business_state(db, case)
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        assert action is not None and log is not None
        if reject_audit:
            assert hits, "The failure hook must intercept the real success SQL"
            assert after == before
            assert response.data.status == action.status == "failed"
            assert log.status == "failed"
            assert action.result_data is None
        else:
            assert after == case.handler_states[0]
            assert response.data.status == action.status == "succeeded"
            assert log.status == "success"
            assert action.finished_at is not None and log.finished_at is not None
            assert action.result_data is not None
