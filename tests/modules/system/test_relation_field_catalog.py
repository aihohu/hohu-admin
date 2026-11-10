"""A candidate catalog cannot confer delegation or activate unfinished adapters."""

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import (
    Access,
    FieldRef,
    Grant,
    RecordContext,
    compile_rules,
    resolve_fields,
)
from app.core.field_policy.documents import catalog_document, load_catalog
from app.modules.system.field_policy import ordinary_catalog
from app.modules.system.service.generic_field_policy_state import affected_objects


def candidate():
    return ordinary_catalog.relation_catalog()


def test_relation_candidate_keeps_production_frozen():
    assert (
        ordinary_catalog.production_catalog().digest
        == "1bba4c34c8f8e6edd636ed8b2447f8fbd0053e7082670846aa11ab25db32cb62"
    )
    assert set(candidate().objects) == set(
        ordinary_catalog.production_catalog().objects
    ) | {"system.role", "system.dept", "system.menu"}


def test_relation_sources_freeze_user_copies():
    catalog = candidate()
    assert "system.user" in affected_objects(catalog, "system.role")
    assert "system.user" in affected_objects(catalog, "system.dept")


def test_special_fields_do_not_have_ordinary_update_grants():
    catalog = candidate()
    for obj, names in {
        "system.role": (
            "role_code",
            "data_scope",
            "status",
            "menu_ids",
            "dept_ids",
            "agent_ids",
        ),
        "system.dept": ("parent_id", "ancestors"),
        "system.menu": (
            "permission",
            "parent_id",
            "component",
            "route_name",
            "query",
            "status",
        ),
    }.items():
        assert obj in catalog.objects
        for name in names:
            assert (
                Access("update", "management.update")
                not in catalog.fields[FieldRef(obj, name)].accesses
            )


def test_source_actions_do_not_require_unrelated_list_button():
    catalog = candidate()
    assert "user.relations" in catalog.actions
    assert catalog.actions["user.relations"].permission_codes == ("system:user:list",)
    assert "system.role" in catalog.actions["user.relations"].object_ids


def test_candidate_roundtrip_and_non_grantable_derivation():
    catalog = candidate()
    assert (
        load_catalog(catalog_document(catalog), expected_digest=catalog.digest).digest
        == catalog.digest
    )
    with pytest.raises(BusinessException):
        compile_rules(
            catalog,
            (
                Grant(
                    0,
                    "201",
                    FieldRef("system.user", "role_names"),
                    Access("read", "management.read"),
                ),
            ),
        )
    assert catalog.actions["role.menus"].kind == "special"
    assert catalog.actions["role.menus"].uses == ()


def test_filter_grant_never_implies_join():
    catalog = candidate()
    query = Access("eq", "management.query", "filter")
    join = Access("eq", "management.query", "join")
    ref = FieldRef("system.user", "roles")
    assert (
        query in catalog.fields[ref].accesses and join in catalog.fields[ref].accesses
    )
    rules = compile_rules(catalog, (Grant(0, "201", ref, query),))
    assert (0, "201", ref, join) not in rules.index


def test_allowing_all_ordinary_fields_cannot_write_special_fields():
    catalog = candidate()
    update = Access("update", "management.update")
    obj = catalog.objects["system.menu"]
    rules = compile_rules(
        catalog,
        tuple(
            Grant(0, "201", FieldRef(obj.object_id, f.field_id), update)
            for f in obj.fields
            if update in f.accesses
        ),
    )
    context = RecordContext(
        0, obj.object_id, "301", frozenset({"201"}), frozenset({"system:menu:edit"})
    )
    decisions = resolve_fields(catalog, rules, context, "menu.update", update)
    assert decisions["menu_name"].allowed
    assert not decisions["permission"].allowed and not decisions["route_name"].allowed
