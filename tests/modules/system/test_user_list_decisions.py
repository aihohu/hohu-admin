from app.core.field_policy import FieldRef, Grant
from app.modules.system.field_policy.list_admission import FieldRoles
from app.modules.system.field_policy.relation_catalog import CREATE, UPDATE
from app.modules.system.field_policy.user_list_policy import READ, selected_grants


def test_point_decision_uses_only_applicable_roles_and_own_tenant():
    ref = FieldRef("system.user", "nickname")
    grants = (
        Grant(0, "201", ref, READ),
        Grant(0, "202", ref, READ, "deny"),
        Grant(1, "203", ref, READ),
    )
    indexed = FieldRoles(0, grants)
    assert indexed.permits(0, {"201"}, ref, READ)
    assert not indexed.permits(0, {"201", "202"}, ref, READ)
    assert not indexed.permits(0, {"203"}, ref, READ)
    assert not indexed.permits(1, {"201"}, ref, READ)
    assert not indexed.permits(0, set(), ref, READ)


def test_follow_can_grant_masked_phone_without_phone_query():
    policy = {
        "mode": "restrict_selected",
        "read": [],
        "query": [],
        "sensitive": {"read": ["user_phone"], "query": []},
    }
    phone = [
        g for g in selected_grants(0, 201, policy) if g.ref.field_id == "user_phone"
    ]
    assert [(g.access, g.effect, g.level) for g in phone] == [(READ, "allow", "masked")]


def test_create_update_choices_do_not_change_older_documents_or_other_uses():
    base = {"mode": "allow_selected", "read": ["nickname"], "query": []}
    assert all(g.access not in (CREATE, UPDATE) for g in selected_grants(0, 201, base))
    extended = base | {"create": ["nickname"], "update": ["user_name"]}
    grants = selected_grants(0, 201, extended)
    assert {(g.ref.field_id, g.access) for g in grants} == {
        ("nickname", READ),
        ("nickname", CREATE),
        ("user_name", UPDATE),
    }
    follow = extended | {
        "mode": "restrict_selected",
        "create": [],
        "update": ["nickname"],
    }
    rules = {
        (g.ref.field_id, g.access): g.effect for g in selected_grants(0, 201, follow)
    }
    assert rules["nickname", CREATE] == "allow"
    assert rules["nickname", UPDATE] == "deny"
    assert ("user_phone", CREATE) not in rules
