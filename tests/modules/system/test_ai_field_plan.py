import pytest

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.ai_plan import ai_query_plan, ai_write_fields


def test_lookup_declares_exact_selectors_before_ambiguity_count():
    plan = ai_query_plan("user.lookup", {"phone": "secret", "nickname": "name"})
    assert {(d.field_id, d.operation) for d in plan.dependencies} == {
        ("user_phone", "exact"),
        ("nickname", "exact"),
    }


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("user.count", {"filters": {"status": "1"}}, {("status", "eq")}),
        ("user.stats", {"group_by": "user_gender"}, {("user_gender", "group")}),
        ("user.distinct", {"field": "status"}, {("status", "distinct")}),
        ("user.batch_delete", {"phones": ["secret"]}, {("user_phone", "exact")}),
    ],
)
def test_complete_query_dependencies(tool, args, expected):
    assert {
        (d.field_id, d.operation) for d in ai_query_plan(tool, args).dependencies
    } == expected


@pytest.mark.parametrize(
    "args", [{"filters": {"password": "secret"}}, {"filters": {"user_email": "secret"}}]
)
def test_query_rejects_fields_outside_original_tool_allowlist(args):
    with pytest.raises(AuthorizationException):
        ai_query_plan("user.list", args)


def test_update_retains_none_means_unchanged():
    assert ai_write_fields(
        "user.update", {"user_id": 3, "nickname": None, "user_phone": "123"}
    ) == {"update": frozenset({"user_phone"})}


def test_assignment_requires_relation_root_and_password_stays_write_only():
    assert ai_write_fields("user.update_roles", {"user_id": 3, "role_ids": []}) == {
        "replace": frozenset({"roles"})
    }
    assert ai_write_fields("user.reset_password", {"user_id": 3}) == {
        "update": frozenset({"password"})
    }


def test_update_does_not_gain_api_only_fields():
    with pytest.raises(AuthorizationException):
        ai_write_fields("user.update", {"user_id": 3, "user_name": "rename"})
