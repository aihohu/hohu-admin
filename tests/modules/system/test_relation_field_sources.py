"""Each relation cell requires both real record scopes and exact provenance."""

from dataclasses import replace

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import FieldRef, Grant, RecordContext, compile_rules
from app.modules.system.field_policy import relation_catalog as module
from app.modules.system.field_policy.ordinary_catalog import relation_catalog


def facts():
    catalog = relation_catalog()
    owner = RecordContext(
        0, "system.user", "101", frozenset({"201"}), frozenset({"system:user:list"})
    )
    source = RecordContext(
        0, "system.role", "202", frozenset({"203"}), owner.permission_codes
    )
    grants = (
        Grant(0, "201", FieldRef("system.user", "roles"), module.READ),
        Grant(0, "203", FieldRef("system.role", "role_name"), module.READ),
        Grant(0, "203", FieldRef("system.role", "i18n_keys"), module.READ),
    )
    return catalog, owner, source, grants


def call(*, output="role_names", owner=None, source=None, grants=None, target="202"):
    catalog, base_owner, base_source, base_grants = facts()
    return module.project_relation_cell(
        catalog,
        compile_rules(catalog, base_grants if grants is None else grants),
        owner=owner or base_owner,
        source=source or base_source,
        target_id=target,
        output=output,
        values={"role_name": "Reviewers", "i18n_keys": {"en": "role.reviewers"}},
    )


def test_visible_cell_retains_both_exact_sources():
    cell = call()
    assert cell.visible and cell.value == "Reviewers"
    assert {(s.record_key, s.ref) for s in cell.sources} == {
        ("101", FieldRef("system.user", "roles")),
        ("202", FieldRef("system.role", "role_name")),
    }


@pytest.mark.parametrize("missing", [0, 1])
def test_relation_or_source_denial_never_returns_plaintext(missing):
    _, _, _, grants = facts()
    cell = call(grants=grants[:missing] + grants[missing + 1 :])
    assert not cell.visible and cell.value is None and cell.sources == ()


def test_other_record_role_scope_cannot_be_borrowed():
    _, _, source, _ = facts()
    assert not call(source=replace(source, role_ids=frozenset({"201"}))).visible


def test_explicit_source_deny_wins_and_i18n_cannot_restore_name():
    _, _, source, grants = facts()
    denied = Grant(
        0, "204", FieldRef("system.role", "role_name"), module.READ, effect="deny"
    )
    cell = call(
        output="role_name_keys",
        source=replace(source, role_ids=frozenset({"203", "204"})),
        grants=grants + (denied,),
    )
    assert not cell.visible and cell.value is None


def test_translation_value_is_frozen_and_has_name_provenance():
    cell = call(output="role_name_keys")
    assert cell.visible and cell.value["en"] == "role.reviewers"
    assert len(cell.sources) == 3
    with pytest.raises(TypeError):
        cell.value["en"] = "changed"


def test_department_source_uses_its_own_scope():
    catalog, owner, _, _ = facts()
    source = RecordContext(
        0, "system.dept", "301", frozenset({"203"}), owner.permission_codes
    )
    grants = (
        Grant(0, "201", FieldRef("system.user", "departments"), module.READ),
        Grant(0, "203", FieldRef("system.dept", "dept_name"), module.READ),
    )
    cell = module.project_relation_cell(
        catalog,
        compile_rules(catalog, grants),
        owner=owner,
        source=source,
        target_id="301",
        output="dept_names",
        values={"dept_name": "Engineering"},
    )
    assert cell.visible and cell.value == "Engineering"
    assert any(s.record_key == "301" for s in cell.sources)


@pytest.mark.parametrize(
    "change", ["tenant", "object", "target", "output", "permission"]
)
def test_invalid_relation_context_fails_closed(change):
    _, _, source, _ = facts()
    args = {
        "tenant": {"source": replace(source, tenant_id=1)},
        "object": {"source": replace(source, object_id="system.dept")},
        "target": {"target": "999"},
        "output": {"output": "password"},
        "permission": {"source": replace(source, permission_codes=frozenset())},
    }[change]
    with pytest.raises(BusinessException):
        call(**args)
