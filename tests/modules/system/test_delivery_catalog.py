"""Every delivery surface is registered without modifying historical catalogs."""

from dataclasses import replace

import pytest

from app.core.field_policy import (
    Access,
    FieldRef,
    RecordContext,
    compile_rules,
    resolve_fields,
)
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.system.field_policy.delivery_catalog import delivery_catalog
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.field_policy.transfer_catalog import READ, transfer_catalog


def delivery():

    return delivery_catalog()


def test_delivery_catalog_preserves_exact_fourteen_object_source():
    old, new = transfer_catalog(), delivery()
    assert len(old.objects) == 14 and len(new.objects) == 23
    assert production_catalog().digest == new.digest
    for key, obj in old.objects.items():
        for old_field in obj.fields:
            current = new.fields[FieldRef(key, old_field.field_id)]
            assert set(old_field.accesses) <= set(current.accesses)
            assert replace(current, accesses=old_field.accesses) == old_field
    assert all(new.actions[key] == action for key, action in old.actions.items())


@pytest.mark.parametrize(
    ("obj", "action"),
    [
        ("system.operation_log", "delivery.operation-list"),
        ("system.login_log", "delivery.login-list"),
        ("job.log", "delivery.job-list"),
        ("ai.agent", "delivery.agent-list"),
        ("ai.conversation", "delivery.conversation-list"),
        ("ai.message", "delivery.message-read"),
        ("ai.operation_log", "delivery.trace-list"),
        ("ai.routing_log", "delivery.feedback-list"),
        ("ai.routing_feedback", "delivery.feedback-list"),
    ],
)
def test_delivery_fields_are_denied_without_explicit_grants(obj, action):
    catalog = delivery()
    record = RecordContext(
        71,
        obj,
        "701",
        frozenset({"201"}),
        frozenset(catalog.actions[action].permission_codes),
    )
    decisions = resolve_fields(
        catalog, compile_rules(catalog, ()), record, action, READ
    )
    assert all(
        not decision.allowed
        for name, decision in decisions.items()
        if not catalog.fields[FieldRef(obj, name)].structural
    )


def test_delivery_has_closed_tools_and_cross_object_sources():

    catalog = delivery()
    registered = ToolRegistry.get().all()
    assert len(registered) == 33
    assert {
        name.removeprefix("ai.tool.")
        for name in catalog.actions
        if name.startswith("ai.tool.")
    } == {tool.meta.name for tool in registered}
    assert FieldRef("system.user", "user_name") in {
        use.ref for use in catalog.actions["delivery.feedback-list"].uses
    }
    assert FieldRef("system.dept", "dept_name") in {
        use.ref for use in catalog.actions["ai.tool.user.dept_lookup"].uses
    }
    assert FieldRef("system.menu", "menu_name") in {
        use.ref for use in catalog.actions["ai.tool.role.menu_lookup"].uses
    }
    assert "ai.tool.arbitrary" not in catalog.actions


@pytest.mark.parametrize(
    ("obj", "name"),
    [
        ("system.operation_log", "request_params"),
        ("system.login_log", "message"),
        ("system.login_log", "user_agent"),
        ("job.log", "error_msg"),
        ("job.log", "runner_id"),
        ("ai.agent", "system_prompt"),
        ("ai.agent", "model_preference"),
        ("ai.message", "tokens_input"),
        ("ai.routing_log", "candidates"),
        ("ai.routing_log", "input_message_hash"),
        ("ai.operation_log", "args_summary"),
    ],
)
def test_internal_and_unknown_free_text_cannot_be_granted(obj, name):
    assert FieldRef(obj, name) not in delivery().fields


def test_actual_ai_lookup_search_and_path_uses_are_explicit_new_grants():

    catalog = delivery()
    contains = Access("contains", "management.query", "filter")
    join = Access("eq", "management.query", "join")
    for name in ("permission", "route_name", "route_path", "i18n_key"):
        assert contains in catalog.fields[FieldRef("system.menu", name)].accesses
    for name in ("parent_id", "ancestors"):
        assert join in catalog.fields[FieldRef("system.dept", name)].accesses
