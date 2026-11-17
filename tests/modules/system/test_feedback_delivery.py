"""Feedback queries cannot recover hidden identity or global Agent labels."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_menus
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.ai.schemas.routing_feedback import FeedbackListQuery
from app.modules.system.ai_tools import (
    dept,  # noqa: F401 - actual production registration
)
from app.modules.system.models.menu import Menu
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def seed(plain):
    async with plain() as db:
        await db.execute(
            insert(AiAgent).values(
                agent_id=3101,
                code="hidden_code",
                name="PLATFORM-SECRET",
                description="PRIVATE",
                enabled=False,
            )
        )
        await db.execute(
            insert(AiRoutingFeedback).values(
                feedback_id=901,
                tenant_id=0,
                message_id=1001,
                user_id=101,
                original_agent="hidden_code",
                feedback="correct",
                create_time=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await db.commit()


async def test_feedback_list_keeps_denied_names_and_unavailable_agent_codes_out(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    await seed(plain)
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.user", "user_name", "read"),)
    )
    page = await AuthorizationRuntime(plain.kw["bind"]).delivery.feedback(
        tenant=TENANT, actor_user_version=1, query=FeedbackListQuery(feedback="all")
    )
    row = page["records"][0]
    assert "userName" not in row and "userId" not in row
    assert "originalAgentName" not in row and "originalAgent" not in row
    assert "PLATFORM-SECRET" not in str(page) and "hidden_code" not in str(page)
    assert row["feedback"] == "correct"


async def test_feedback_filter_is_proved_over_complete_date_domain_before_empty_result(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    await seed(plain)
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing=(("ai.routing_feedback", "feedback", "eq"),),
    )
    with pytest.raises(BusinessException) as caught:
        await AuthorizationRuntime(plain.kw["bind"]).delivery.feedback(
            tenant=TENANT,
            actor_user_version=1,
            query=FeedbackListQuery(feedback="wrong"),
        )
    assert caught.value.error_code == "FIELD_POLICY_DENIED"


@pytest.mark.parametrize("denied_name", [False, True])
async def test_router_candidate_sources_include_only_actual_projected_fields(
    system_sessions, monkeypatch, denied_name
):
    _, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9011,
                menu_name="Chat",
                menu_type="F",
                status="1",
                permission="ai:chat:use",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9011)
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9012,
                menu_name="Departments",
                menu_type="F",
                status="1",
                permission="system:dept:list",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9012)
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=3102,
                code="dept_mgmt",
                name="DENIED-AGENT-NAME",
                description="Allowed purpose",
                enabled=True,
            )
        )
        await db.execute(
            insert(RoleAiAgent).values(
                tenant_id=0, role_id=201, agent_id=3102, enabled=True
            )
        )
        await db.execute(
            insert(AiRoutingFeedback).values(
                feedback_id=901,
                tenant_id=0,
                message_id=1001,
                user_id=101,
                original_agent="dept_mgmt",
                corrected_agent=None,
                feedback="correct",
                create_time=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await db.commit()
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing=(("ai.agent", "name", "read"),) if denied_name else (),
    )
    snapshot = await AuthorizationRuntime(plain.kw["bind"]).delivery.agents(
        tenant=TENANT, actor_user_version=1, with_sources=True
    )
    assert len(snapshot["agents"]) == 1
    assert ("name" not in snapshot["agents"][0]) == denied_name
    groups = snapshot["field_authority"]["source_domains"]
    assert any(
        g.get("object_id") == "ai.agent" and g["records"] == ["3102"] for g in groups
    )
    assert any(["name", "plain"] in g.get("read", ()) for g in groups) != denied_name
    if denied_name:
        assert "DENIED-AGENT-NAME" not in str(snapshot)
    summary = await AuthorizationRuntime(plain.kw["bind"]).delivery.feedback(
        tenant=TENANT, actor_user_version=1, days=7
    )
    if denied_name:
        assert "topWrongAgents" not in summary
    else:
        assert summary["topWrongAgents"] == []
