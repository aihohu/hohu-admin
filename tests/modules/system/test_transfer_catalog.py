"""Transfer declarations preserve old catalogs and never turn tasks into grants."""

from dataclasses import replace

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
from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.field_policy.transfer_catalog import (
    transfer_catalog,
    transfer_user_grants,
)

READ = Access("read", "management.read")


def test_transfer_catalog_preserves_ten_object_source_and_original_digest():
    old, new = control_catalog(), transfer_catalog()
    assert len(old.objects) == 10 and len(new.objects) == 14
    assert (
        new.digest == "6e8b05d78ef00aba652db283a3529fc0ccf58c0bd5bb93743d2c7d094cc61184"
    )
    for key, value in old.objects.items():
        current = new.objects[key]
        assert len(current.fields) == len(value.fields)
        for before, after in zip(value.fields, current.fields, strict=True):
            assert set(before.accesses) <= set(after.accesses)
            assert replace(after, accesses=before.accesses) == before
    for key, value in old.actions.items():
        assert new.actions[key] == value
    assert {
        "system.file",
        "transfer.import",
        "transfer.export",
        "transfer.audit",
    } <= new.objects.keys()


@pytest.mark.parametrize(
    "obj", ["system.file", "transfer.import", "transfer.export", "transfer.audit"]
)
def test_unknown_new_object_has_no_business_read_even_with_function(obj):
    catalog = transfer_catalog()
    action = {
        "system.file": "file.list",
        "transfer.import": "transfer.import-list",
        "transfer.export": "transfer.export-list",
        "transfer.audit": "transfer.audit-list",
    }[obj]
    record = RecordContext(
        71,
        obj,
        "701",
        frozenset({"201"}),
        frozenset(catalog.actions[action].permission_codes),
    )
    decision = resolve_fields(catalog, compile_rules(catalog, ()), record, action, READ)
    assert all(
        not d.allowed
        for name, d in decision.items()
        if not catalog.fields[FieldRef(obj, name)].structural
    )


def test_file_storage_metadata_is_never_grantable():
    catalog = transfer_catalog()
    for name in (
        "file_path",
        "file_name",
        "file_url",
        "storage_key",
        "sha256",
        "owner_user_id",
        "business_id",
    ):
        assert FieldRef("system.file", name) not in catalog.fields
    for obj in ("transfer.import", "transfer.export", "transfer.audit"):
        for name in (
            "filter_snapshot",
            "reason",
            "preview_token",
            "records_hash",
            "detail",
            "error_message",
        ):
            assert FieldRef(obj, name) not in catalog.fields


@pytest.mark.parametrize(
    "access",
    [
        READ,
        Access("export", "export"),
        Access("create", "import"),
        Access("update", "import"),
        Access("exact", "import", "match"),
    ],
)
def test_generic_user_rule_keeps_operation_purpose_and_level(access):
    rule = Grant(
        71,
        "201",
        FieldRef("system.user", "user_email"),
        access,
        level="masked" if access.operation in {"read", "export"} else "plain",
        mask_id="email/v1" if access.operation in {"read", "export"} else None,
    )
    converted = transfer_user_grants(71, (rule,))
    assert len(converted) == 1
    got = converted[0]
    assert (
        got.role_id,
        got.field_id,
        got.operation,
        got.purpose,
        got.read,
        got.mask_id,
    ) == (
        "201",
        "user_email",
        access.operation,
        access.purpose,
        rule.level,
        rule.mask_id,
    )


def test_generic_bridge_does_not_broaden_eq_filter_into_join():
    ref = FieldRef("system.user", "departments")
    filtered = Grant(71, "201", ref, Access("eq", "management.query", "filter"))
    assert transfer_user_grants(71, (filtered,)) == ()
    joined = Grant(71, "201", ref, Access("eq", "management.query", "join"))
    assert len(transfer_user_grants(71, (filtered, joined))) == 1
    denied = Grant(71, "201", ref, joined.access, "deny")
    result = transfer_user_grants(71, (filtered, denied))
    assert len(result) == 1 and result[0].effect == "deny"


def test_bridge_rejects_cross_tenant_unknown_and_duplicate_rules():
    rule = Grant(71, "201", FieldRef("system.user", "nickname"), READ)
    for rules in (
        (rule, rule),
        (Grant(72, "201", rule.ref, READ),),
        (Grant(71, "201", FieldRef("system.user", "unknown"), READ),),
    ):
        with pytest.raises(BusinessException):
            transfer_user_grants(71, rules)


def test_bridge_does_not_turn_source_object_rules_into_user_permissions():
    rule = Grant(71, "201", FieldRef("system.role", "role_name"), READ)
    assert transfer_user_grants(71, (rule,)) == ()


def test_transfer_actions_bind_relation_sources_and_function_permissions():
    catalog = transfer_catalog()
    action = catalog.actions["transfer.user-export"]
    assert action.permission_codes == ("system:user:export",)
    assert {u.ref for u in action.uses} >= {
        FieldRef("system.dept", "dept_name"),
        FieldRef("system.role", "role_code"),
    }
    for name in (
        "transfer.user-import",
        "transfer.template",
        "transfer.error-download",
        "transfer.export-download",
        "file.download",
        "file.upload",
    ):
        assert catalog.actions[name].permission_codes
