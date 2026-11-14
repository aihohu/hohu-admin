"""Actual production Gateway readers must prove relation sources before handlers."""

import pytest
from sqlalchemy import insert, update

from app.core.authorization_plan import TenantLock
from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_menus
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.delivery_source_domains import (
    source_domain,
    validate_receipt,
    with_sources,
)
from app.modules.ai.service.field_authority_runtime import ai_field_authority
from app.modules.ai.service.result_projection_service import (
    DATA_SCOPE_RESOLVER_VERSION,
    result_projection_service,
)
from app.modules.auth.service import load_live_user_authority
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def setup_call(sessions, monkeypatch, *, tool, missing):
    factory, plain = sessions
    load_builtin_tools()
    code = ToolRegistry.get().find(tool).meta.agent
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                dept_id=501,
                tenant_id=0,
                dept_name="PROTECTED DEPARTMENT",
                ancestors="0",
                status="1",
            )
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=3101,
                code=code,
                name="Visible Agent",
                description="Test",
                enabled=True,
            )
        )
        await db.execute(
            insert(RoleAiAgent).values(
                tenant_id=0, role_id=201, agent_id=3101, enabled=True
            )
        )
        for number, permission in enumerate(
            ("ai:chat:use", *ToolRegistry.get().find(tool).meta.required_perms), 20001
        ):
            await db.execute(
                insert(Menu).values(
                    menu_id=number,
                    tenant_id=0,
                    menu_name=permission,
                    menu_type="F",
                    permission=permission,
                    status="1",
                )
            )
            await db.execute(
                insert(role_menus).values(tenant_id=0, role_id=201, menu_id=number)
            )
        await db.commit()
    await activate_relations(sessions, monkeypatch, missing=missing)
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            authority = with_sources(
                await ai_field_authority.current(db, tx, tenant=TENANT),
                (
                    source_domain(
                        action_id="delivery.message-read",
                        object_id="ai.message",
                        records=(1102,),
                        read=(("content", "plain"),),
                    ),
                ),
            )
            actor = await load_live_user_authority(db, tenant=TENANT)
            scope_hash = await result_projection_service.compute_data_scope_hash(
                db, actor
            )
        await db.rollback()
    async with plain() as db:
        await db.execute(
            insert(AiConversation).values(
                conversation_id=1101,
                tenant_id=0,
                user_id=101,
                title="New test",
                title_origin="authored",
                model_name="fixture",
            )
        )
        await db.execute(
            insert(AiMessage).values(
                message_id=1102,
                tenant_id=0,
                conversation_id=1101,
                role="user",
                content="test request",
                is_active=True,
                trace_id="delivery_test",
                agent_code=code,
                field_authority=authority,
                projection_dependency_message_ids=[],
            )
        )
        await db.commit()
    return {
        "tenant": TENANT,
        "actor_user_version": 1,
        "conversation_id": 1101,
        "source_id": 1102,
        "trace_id": "delivery_test",
        "agent_code": code,
        "scope_hash": scope_hash,
        "tool": tool,
    }


@pytest.mark.parametrize(
    "tool,missing,args",
    [
        ("dept.list", (("system.dept", "dept_name", "read"),), {}),
        ("role.list", (("system.role", "role_name", "read"),), {}),
        (
            "role.count",
            (("system.role", "status", "eq"),),
            {"filters": {"status": "2"}},
        ),
    ],
)
async def test_relation_source_deny_blocks_actual_gateway_before_output(
    system_sessions, monkeypatch, tool, missing, args
):
    call = await setup_call(system_sessions, monkeypatch, tool=tool, missing=missing)
    with pytest.raises(BusinessException) as caught:
        await AuthorizationRuntime(system_sessions[1].kw["bind"]).chat.read_tool(
            **call, args=args
        )
    assert caught.value.error_code in {
        "FIELD_QUERY_DENIED",
        "FIELD_READ_DENIED",
        "FIELD_PERMISSION_DENIED",
    }


@pytest.mark.parametrize("role", ["user", "assistant"])
async def test_current_message_read_deny_hides_original_and_generated_bodies(
    system_sessions, monkeypatch, role
):
    call = await setup_call(
        system_sessions,
        monkeypatch,
        tool="user.count",
        missing=(("ai.message", "content", "read"),),
    )
    _, plain = system_sessions
    async with plain() as db:
        if role == "assistant":
            source = await db.get(AiMessage, 1102)
            await db.execute(
                insert(AiMessage).values(
                    message_id=1103,
                    tenant_id=0,
                    conversation_id=1101,
                    role="assistant",
                    content="PROTECTED GENERATED BODY",
                    is_active=True,
                    trace_id="delivery_test",
                    agent_code=call["agent_code"],
                    field_authority=source.field_authority,
                    tool_codes=[],
                    subject_refs=[],
                    subject_refs_hash=result_projection_service.subject_refs_hash(()),
                    resolver_version=DATA_SCOPE_RESOLVER_VERSION,
                    data_scope_hash=call["scope_hash"],
                    projection_dependency_message_ids=[],
                )
            )
        else:
            await db.execute(
                update(AiMessage)
                .where(AiMessage.message_id == 1102)
                .values(content="PROTECTED ORIGINAL BODY")
            )
        await db.commit()
    outputs = await AuthorizationRuntime(plain.kw["bind"]).chat.messages(
        tenant=TENANT, actor_user_version=1, conversation_id=1101
    )
    assert "PROTECTED" not in str(outputs)
    assert any(getattr(row, "status", None) == "redacted" for row in outputs)


async def test_actual_gateway_captures_root_and_entire_aggregate_domain(
    system_sessions, monkeypatch
):
    call = await setup_call(system_sessions, monkeypatch, tool="role.count", missing=())
    result = await AuthorizationRuntime(system_sessions[1].kw["bind"]).chat.read_tool(
        **call, args={}
    )
    assert result.ok
    receipt = result.projection.field_authority
    assert validate_receipt(receipt) is not None
    assert any(
        g.get("object_id") == "ai.message" and g["records"] == ["1102"]
        for g in receipt["source_domains"]
    )
    assert any(
        g.get("object_id") == "system.role" and "201" in g["records"]
        for g in receipt["source_domains"]
    )


async def test_watermark_only_old_source_cannot_start_a_new_tool(
    system_sessions, monkeypatch
):
    call = await setup_call(system_sessions, monkeypatch, tool="role.count", missing=())
    async with system_sessions[1]() as db:
        source = await db.get(AiMessage, 1102)
        source.field_authority = source.field_authority["authority"]
        await db.commit()
    with pytest.raises(BusinessException) as caught:
        await AuthorizationRuntime(system_sessions[1].kw["bind"]).chat.read_tool(
            **call, args={}
        )
    assert caught.value.error_code == "FIELD_CONFIRMATION_LINEAGE_REQUIRED"
