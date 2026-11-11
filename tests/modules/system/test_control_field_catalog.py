"""Candidate declarations must never silently activate new production objects."""

from datetime import datetime

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import (
    Access,
    FieldRef,
    Grant,
    RecordContext,
    compile_rules,
    normalize_paths,
    resolve_fields,
    validate_write,
)
from app.core.field_policy.documents import catalog_document, load_catalog
from app.modules.system.field_policy.control_catalog import (
    NEXT_RUN_SOURCES,
    control_catalog,
    project_next_run,
)
from app.modules.system.field_policy.ordinary_catalog import (
    ordinary_catalog,
    production_catalog,
    relation_catalog,
)
from app.modules.system.settings_catalog import SETTINGS

READ = Access("read", "management.read")
UPDATE = Access("update", "management.update")


def record(obj, permissions, *, tenant=0, roles=("201",)):
    return RecordContext(tenant, obj, "301", frozenset(roles), frozenset(permissions))


def all_rules(catalog, obj):
    return compile_rules(
        catalog,
        tuple(
            Grant(0, "201", FieldRef(obj, field.field_id), access)
            for field in catalog.objects[obj].fields
            for access in field.accesses
        ),
    )


def test_candidate_keeps_all_historical_and_production_facts():
    candidate = control_catalog()
    assert (
        ordinary_catalog().digest
        == "1bba4c34c8f8e6edd636ed8b2447f8fbd0053e7082670846aa11ab25db32cb62"
    )
    assert (
        relation_catalog().digest
        == "0cc4f21cfd52b83d1ee018a2009034cf546d437d93074716bfdb80c82e3d0ec6"
    )
    assert production_catalog().digest == relation_catalog().digest
    assert (len(candidate.objects), len(candidate.fields)) == (10, 144)
    for key, value in relation_catalog().objects.items():
        assert candidate.objects[key] == value
    for key, value in relation_catalog().actions.items():
        assert candidate.actions[key] == value
    assert (
        load_catalog(
            catalog_document(candidate), expected_digest=candidate.digest
        ).digest
        == candidate.digest
    )


def test_settings_are_exact_keys_and_fixed_domains_are_not_grantable():
    catalog = control_catalog()
    fixed = {
        "ai:enabled_tools",
        *[key for key in SETTINGS if key.startswith("security:")],
    }
    assert {
        f.field_id for f in catalog.objects["system.setting"].fields
    } == SETTINGS.keys() - fixed
    assert all(o.domain_kind == "tenant" for o in catalog.objects.values())
    assert not any(
        key.startswith("platform.") or key == "system.mail" for key in catalog.objects
    )
    for key in (
        "setting_value",
        "siteName",
        "*",
        "upload:maxBytes",
        "ai:enabled_tools",
    ):
        with pytest.raises(BusinessException):
            normalize_paths(catalog, "system.setting", (key,))
    secret = catalog.fields[FieldRef("system.setting", "auth:default_password")]
    assert secret.accesses == (UPDATE,) and secret.read_cap == "hidden"


@pytest.mark.parametrize(
    "obj,action,permission,accepted,rejected",
    [
        (
            "system.config",
            "config.update",
            "system:config:edit",
            "config_name",
            "is_public",
        ),
        (
            "job.definition",
            "job.update",
            "system:job:edit",
            "job_name",
            "cron_expression",
        ),
        (
            "system.setting",
            "settings.save",
            "system:setting:edit",
            "site_name",
            "registration_role_id",
        ),
    ],
)
def test_full_field_allow_still_cannot_use_an_ordinary_command_for_special_writes(
    obj, action, permission, accepted, rejected
):
    catalog = control_catalog()
    rules = all_rules(catalog, obj)
    context = record(obj, (permission,))
    assert validate_write(catalog, rules, context, action, UPDATE, (accepted,)) == (
        accepted,
    )
    with pytest.raises(BusinessException):
        validate_write(catalog, rules, context, action, UPDATE, (rejected,))


def test_publication_needs_its_action_permission_even_with_every_field_granted():
    catalog = control_catalog()
    rules = all_rules(catalog, "system.config")
    with pytest.raises(BusinessException):
        resolve_fields(
            catalog,
            rules,
            record("system.config", ("system:config:edit",)),
            "config.publish",
            READ,
        )
    action = catalog.actions["config.publish"]
    assert (
        action.kind == "special" and "system:config:publish" in action.permission_codes
    )


def test_scheduling_and_run_are_distinct_and_job_key_is_immutable():
    catalog = control_catalog()
    obj = "job.definition"
    rules = all_rules(catalog, obj)
    context = record(obj, ("system:job:edit",))
    assert validate_write(
        catalog, rules, context, "job.schedule", UPDATE, ("cron_expression",)
    ) == ("cron_expression",)
    for action in ("job.update", "job.schedule", "job.status"):
        with pytest.raises(BusinessException):
            validate_write(catalog, rules, context, action, UPDATE, ("job_key",))
    with pytest.raises(BusinessException):
        resolve_fields(catalog, rules, context, "job.run", READ)


def test_next_run_is_not_independently_grantable_and_carries_all_real_sources():
    catalog = control_catalog()
    ref = FieldRef("job.definition", "next_run_time")
    assert {r.field_id for r in catalog.fields[ref].sources} == set(NEXT_RUN_SOURCES)
    with pytest.raises(BusinessException):
        compile_rules(catalog, (Grant(0, "201", ref, READ),))
    values = {
        "status": "1",
        "trigger_type": "cron",
        "cron_expression": "* * * * *",
        "interval_value": None,
        "interval_unit": None,
    }
    context = record("job.definition", ("system:job:list",))
    now = datetime(2026, 11, 11, 12)
    rules = all_rules(catalog, "job.definition")
    cell = project_next_run(catalog, rules, context, values, now)
    assert cell.visible and cell.value == now
    assert {s.ref.field_id for s in cell.sources} == set(NEXT_RUN_SOURCES)
    assert all(
        s.tenant_id == 0
        and s.record_key == "301"
        and s.catalog_digest == catalog.digest
        for s in cell.sources
    )
    null = project_next_run(catalog, rules, context, values, None)
    assert null.visible and null.value is None


@pytest.mark.parametrize(
    "missing",
    ["status", "trigger_type", "cron_expression", "interval_value", "interval_unit"],
)
def test_any_source_denial_hides_the_entire_derived_cell(missing):
    catalog = control_catalog()
    obj = "job.definition"
    grants = tuple(
        Grant(0, "201", FieldRef(obj, name), READ) for name in NEXT_RUN_SOURCES
    )
    rules = compile_rules(
        catalog, grants + (Grant(0, "202", FieldRef(obj, missing), READ, "deny"),)
    )
    context = record(obj, ("system:job:list",), roles=("201", "202"))
    cell = project_next_run(catalog, rules, context, {}, datetime.now())
    assert not cell.visible and cell.value is None and not cell.sources


def test_no_candidate_default_grant_and_no_cross_tenant_source():
    catalog = control_catalog()
    context = record("job.definition", ("system:job:list",), tenant=7)
    cell = project_next_run(
        catalog, all_rules(catalog, "job.definition"), context, {}, None
    )
    assert not cell.visible
    empty = compile_rules(catalog, ())
    assert not resolve_fields(
        catalog,
        empty,
        record("system.config", ("system:config:list",)),
        "config.list",
        READ,
    )["config_value"].allowed


def test_import_match_and_export_do_not_inherit_manual_write_or_read():
    catalog = control_catalog()
    key = catalog.fields[FieldRef("system.config", "config_key")]
    assert Access("exact", "import", "match") in key.accesses
    assert Access("create", "import") in key.accesses
    ref = FieldRef("system.config", "config_value")
    rules = compile_rules(catalog, (Grant(0, "201", ref, READ),))
    context = record("system.config", ("system:config:export",))
    assert not resolve_fields(
        catalog, rules, context, "config.export", Access("export", "export")
    )["config_value"].allowed
