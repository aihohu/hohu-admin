"""Closed generic contracts: no production registration or persistence implied."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from importlib import import_module

import pytest
from sqlalchemy import Boolean, column, false, true

from app.core.exceptions import BusinessException


@pytest.mark.parametrize(
    "value_type,value,ok",
    [
        ("id", 2**63 - 1, True),
        ("id", True, False),
        ("id", "01", False),
        ("id", 0, False),
        ("integer", True, False),
        ("number", float("nan"), False),
        ("number", float("inf"), False),
        ("boolean", 1, False),
        ("string", 1, False),
        ("date", date(2026, 11, 6), True),
        ("date", datetime.now(UTC), False),
        ("datetime", datetime.now(UTC), True),
        ("json", {"a": [1, None]}, True),
        ("json", {"a": datetime.now(UTC)}, False),
        ("json", {1: "bad key"}, False),
    ],
)
def test_projection_rejects_type_confusion_and_non_json_values(
    api, value_type, value, ok
):
    read = api.Access("read", "management.read")
    ref = api.FieldRef("demo.item", "value")
    obj = api.ObjectSpec(
        ref.object_id,
        "tenant",
        "demo.scope",
        (api.FieldSpec("value", value_type, (read,)),),
    )
    action = api.ActionSpec(
        "demo.read", (ref.object_id,), (api.FieldUse(ref, read),), ("demo:item:read",)
    )
    catalog = api.Catalog((obj,), (action,))
    ctx = api.RecordContext(
        0, ref.object_id, "1", frozenset({"1"}), frozenset({"demo:item:read"})
    )
    rules = api.compile_rules(catalog, (api.Grant(0, "1", ref, read),))
    if ok:
        result = api.project(
            catalog, rules, ctx, action.action_id, read, {"value": value}
        )
        assert "value" in result
    else:
        rejected(
            lambda: api.project(
                catalog, rules, ctx, action.action_id, read, {"value": value}
            )
        )


def test_sources_cannot_borrow_dependencies_from_unrelated_sibling(api, setup):
    catalog, _, ref, read, *_ = setup
    obj = catalog.objects[ref.object_id]
    dep = api.FieldRef(ref.object_id, "id")
    obj = replace(
        obj,
        fields=tuple(
            replace(f, sources=(dep,)) if f.field_id == ref.field_id else f
            for f in obj.fields
        ),
    )
    catalog = api.Catalog((obj,), ())
    source = api.SourceRef(
        "tenant", 0, "100", ref, read, catalog.digest, "projection/1"
    )
    dependency = replace(source, ref=dep)
    nodes = (
        api.SourceNode("broken-copy", (source,), ()),
        api.SourceNode("unrelated", (dependency,), ()),
        api.SourceNode("root", (), ("broken-copy", "unrelated")),
    )
    rejected(
        lambda: api.collect_sources(
            catalog, nodes, ("root",), tenant_id=0, projection_version="projection/1"
        )
    )
    valid = (
        api.SourceNode("original", (dependency,), ()),
        api.SourceNode("copy", (source,), ("original",)),
    )
    assert (
        len(
            api.collect_sources(
                catalog,
                valid,
                ("copy",),
                tenant_id=0,
                projection_version="projection/1",
            )
        )
        == 2
    )


def test_escaped_payload_byte_budget_and_invalid_unicode_fail_closed(api, setup):
    catalog, ctx, ref, read, *_ = setup
    rules = api.compile_rules(catalog, (grant(api, ref, read),))
    rejected(
        lambda: api.project(
            catalog, rules, ctx, "user.manage", read, {"phone": "\x00" * 200000}
        ),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )
    rejected(
        lambda: api.project(
            catalog, rules, ctx, "user.manage", read, {"phone": "\ud800"}
        )
    )


def test_source_and_catalog_depth_limits_and_duplicate_input_budget(api, setup):
    catalog, _, ref, read, *_ = setup
    source = api.SourceRef(
        "tenant", 0, "100", ref, read, catalog.digest, "projection/1"
    )
    nodes = tuple(
        api.SourceNode(
            "node" + str(i),
            (source,) if i == 0 else (),
            ("node" + str(i - 1),) if i else (),
        )
        for i in range(17)
    )
    rejected(
        lambda: api.collect_sources(
            catalog, nodes, ("node16",), tenant_id=0, projection_version="projection/1"
        ),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )
    node = api.SourceNode("huge", (source,) * 16385, ())
    rejected(
        lambda: api.collect_sources(
            catalog, (node,), ("huge",), tenant_id=0, projection_version="projection/1"
        ),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )
    fields = tuple(
        api.FieldSpec(
            "f" + str(i),
            "string",
            (read,),
            sources=(api.FieldRef("demo.item", "f" + str(i - 1)),) if i else (),
        )
        for i in range(17)
    )
    rejected(
        lambda: api.Catalog(
            (api.ObjectSpec("demo.item", "tenant", "demo.scope", fields),), ()
        ),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


@pytest.fixture
def api():
    return import_module("app.core.field_policy")


@pytest.fixture
def setup(api):
    read = api.Access("read", "management.read")
    export = api.Access("export", "export")
    update = api.Access("update", "management.update")
    query = api.Access("contains", "management.query", "filter")
    ref = api.FieldRef("system.user", "phone")
    fields = (
        api.FieldSpec("id", "id", (read, export), structural=True),
        api.FieldSpec(
            "phone",
            "string",
            (read, export, update, query),
            nullable=True,
            aliases=("userPhone",),
            mask_id="phone/v1",
        ),
        api.FieldSpec("secret", "string", (update,)),
    )
    obj = api.ObjectSpec("system.user", "tenant", "user.scope", fields)
    uses = tuple(
        api.FieldUse(api.FieldRef(obj.object_id, f.field_id), a)
        for f in fields
        for a in f.accesses
    )
    action = api.ActionSpec(
        "user.manage", (obj.object_id,), uses, ("system:user:list",)
    )
    catalog = api.Catalog((obj,), (action,))
    ctx = api.RecordContext(
        0, obj.object_id, "100", frozenset({"1"}), frozenset({"system:user:list"})
    )
    return catalog, ctx, ref, read, export, update, query


def rejected(call, code="FIELD_POLICY_INVALID"):
    with pytest.raises(BusinessException) as exc:
        call()
    assert exc.value.error_code == code


def grant(api, ref, access, role="1", effect="allow", level="plain", tenant=0):
    return api.Grant(tenant, role, ref, access, effect, level)


def test_catalog_digest_is_order_independent_and_deeply_immutable(api, setup):
    catalog, *_ = setup
    obj = catalog.objects["system.user"]
    action = catalog.actions["user.manage"]
    reordered = api.Catalog(
        (replace(obj, fields=tuple(reversed(obj.fields))),),
        (replace(action, uses=tuple(reversed(action.uses))),),
    )
    assert reordered.digest == catalog.digest
    with pytest.raises(TypeError):
        catalog.objects["oops"] = obj
    with pytest.raises(FrozenInstanceError):
        obj.domain_kind = "platform"


@pytest.mark.parametrize(
    "change",
    [
        {"aliases": ("id",)},
        {"aliases": ("userPhone", "userPhone")},
        {"mask_id": "custom/script"},
        {"value_type": "orm"},
        {"structural": True},
        {"accesses": []},
        {"read_cap": "raw"},
    ],
)
def test_catalog_rejects_unsafe_field_definitions(api, setup, change):
    catalog, *_ = setup
    obj = catalog.objects["system.user"]
    rejected(
        lambda: api.Catalog(
            (replace(obj, fields=(obj.fields[0], replace(obj.fields[1], **change))),),
            (),
        )
    )


@pytest.mark.parametrize(
    "args",
    [
        ("read", "management.query", "filter"),
        ("contains", "management.query", "sort"),
        ("exact", "import", "filter"),
        ("read", "management.read", "filter"),
        ("delete", "management.update", ""),
        ("aggregate", "management.query", "filter"),
    ],
)
def test_access_is_closed(api, args):
    rejected(lambda: api.Access(*args))


def test_unknown_dependencies_cycles_and_export_without_read_rejected(api, setup):
    catalog, _, ref, read, export, *_ = setup
    obj = catalog.objects[ref.object_id]
    phone = obj.fields[1]
    for dep in (api.FieldRef(ref.object_id, "missing"), ref):
        rejected(
            lambda dep=dep: api.Catalog(
                (replace(obj, fields=(replace(phone, sources=(dep,)),)),), ()
            )
        )
    action = api.ActionSpec(
        "bad.export",
        (ref.object_id,),
        (api.FieldUse(ref, export),),
        ("system:user:export",),
    )
    rejected(lambda: api.Catalog((obj,), (action,)))
    rejected(lambda: api.Catalog((obj, obj), ()))
    rejected(lambda: api.Catalog((obj,), (replace(action, kind="navigation"),)))


def test_catalog_limits_are_checked_before_duplicate_collapse(api, setup):
    catalog, *_ = setup
    obj = catalog.objects["system.user"]
    rejected(lambda: api.Catalog((obj,) * 65, ()), "AUTHORIZATION_PLAN_LIMIT_EXCEEDED")
    rejected(
        lambda: api.Catalog((replace(obj, fields=(obj.fields[0],) * 257),), ()),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


def test_default_deny_structural_id_and_functional_gate(api, setup):
    catalog, ctx, _, read, *_ = setup
    rules = api.compile_rules(catalog, ())
    result = api.resolve_fields(catalog, rules, ctx, "user.manage", read)
    assert result["id"].level == "plain"
    assert not result["phone"].allowed
    rejected(
        lambda: api.resolve_fields(
            catalog,
            rules,
            replace(ctx, permission_codes=frozenset()),
            "user.manage",
            read,
        ),
        "FIELD_POLICY_DENIED",
    )
    rejected(lambda: api.resolve_fields(catalog, rules, ctx, "unknown.button", read))


def test_record_scopes_cannot_be_combined_across_roles_or_tenants(api, setup):
    catalog, ctx, ref, read, *_ = setup
    rules = api.compile_rules(
        catalog,
        (
            grant(api, ref, read, level="masked"),
            grant(api, ref, read, role="2"),
            grant(api, ref, read, role="3", effect="deny"),
            grant(api, ref, read, role="1", tenant=1),
        ),
    )

    def resolve(c):
        return api.resolve_fields(catalog, rules, c, "user.manage", read)["phone"]

    assert resolve(ctx).level == "masked"
    assert resolve(replace(ctx, role_ids=frozenset({"1", "2"}))).level == "plain"
    assert not resolve(replace(ctx, role_ids=frozenset({"1", "3"}))).allowed
    assert not resolve(replace(ctx, tenant_id=2)).allowed


def test_read_export_caps_and_query_are_independent(api, setup):
    catalog, ctx, ref, read, export, _, query = setup
    rules = api.compile_rules(
        catalog, (grant(api, ref, read, level="masked"), grant(api, ref, export))
    )
    assert (
        api.resolve_fields(catalog, rules, ctx, "user.manage", export)["phone"].level
        == "masked"
    )
    assert not api.resolve_fields(catalog, rules, ctx, "user.manage", query)[
        "phone"
    ].allowed
    only_export = api.compile_rules(catalog, (grant(api, ref, export),))
    assert not api.resolve_fields(catalog, only_export, ctx, "user.manage", export)[
        "phone"
    ].allowed
    action = replace(catalog.actions["user.manage"], read_caps=((ref, "masked"),))
    limited = api.Catalog(tuple(catalog.objects.values()), (action,))
    rejected(lambda: api.resolve_fields(limited, rules, ctx, "user.manage", read))
    plain = api.compile_rules(limited, (grant(api, ref, read),))
    assert (
        api.resolve_fields(limited, plain, ctx, "user.manage", read)["phone"].level
        == "masked"
    )


def test_rules_reject_unknown_duplicate_fixed_domain_and_invalid_masks(api, setup):
    catalog, _, ref, read, _, update, _ = setup
    valid = grant(api, ref, read)
    for rules in (
        (valid, valid),
        (replace(valid, ref=api.FieldRef(ref.object_id, "unknown")),),
        (replace(valid, access=update, level="masked"),),
        (replace(valid, effect="inherit"),),
    ):
        rejected(lambda rules=rules: api.compile_rules(catalog, rules))
    obj = replace(catalog.objects[ref.object_id], domain_kind="platform")
    fixed = api.Catalog((obj,), ())
    rejected(lambda: api.compile_rules(fixed, (valid,)))
    small = api.Catalog((replace(catalog.objects[ref.object_id], max_rules=1),), ())
    rejected(
        lambda: api.compile_rules(small, (valid, replace(valid, access=update))),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


def test_projection_masks_omits_secrets_preserves_null_and_stringifies_id(api, setup):
    catalog, ctx, ref, read, *_ = setup
    rules = api.compile_rules(catalog, (grant(api, ref, read, level="masked"),))
    out = api.project(
        catalog,
        rules,
        ctx,
        "user.manage",
        read,
        {"id": 100, "phone": "13800138000", "secret": "never"},
    )
    assert out == {"id": "100", "phone": "138****8000"}
    assert api.project(catalog, rules, ctx, "user.manage", read, {"phone": None}) == {
        "phone": None
    }
    with pytest.raises(TypeError):
        out["id"] = "200"
    rejected(
        lambda: api.project(catalog, rules, ctx, "user.manage", read, {"surprise": 1})
    )
    rejected(
        lambda: api.project(
            catalog, rules, ctx, "user.manage", read, {"phone": object()}
        )
    )
    rejected(
        lambda: api.project(
            catalog, rules, ctx, "user.manage", read, {"phone": "x" * (1024 * 1024 + 1)}
        ),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


def test_write_checks_presence_and_alias_duplicates_even_for_null(api, setup):
    catalog, ctx, ref, _, _, update, _ = setup
    rules = api.compile_rules(catalog, ())
    rejected(
        lambda: api.validate_write(
            catalog, rules, ctx, "user.manage", update, ("userPhone",)
        ),
        "FIELD_POLICY_DENIED",
    )
    permitted = api.compile_rules(catalog, (grant(api, ref, update),))
    assert api.validate_write(
        catalog, permitted, ctx, "user.manage", update, ("userPhone",)
    ) == ("phone",)
    rejected(
        lambda: api.normalize_paths(catalog, ref.object_id, ("phone", "userPhone"))
    )
    rejected(lambda: api.normalize_paths(catalog, ref.object_id, ("phone.value",)))


def test_query_uses_scoped_grants_and_has_no_read_fallback(api, setup):
    catalog, ctx, ref, read, _, _, query = setup
    rules = api.compile_rules(catalog, (grant(api, ref, read),))
    predicate = api.query_counterexample(
        catalog,
        rules,
        ctx,
        "user.manage",
        (api.FieldUse(ref, query),),
        true(),
        {"1": true()},
    )
    assert str(predicate.compile(compile_kwargs={"literal_binds": True})) == "true"
    rules = api.compile_rules(catalog, (grant(api, ref, query),))
    predicate = api.query_counterexample(
        catalog,
        rules,
        ctx,
        "user.manage",
        (api.FieldUse(ref, query),),
        true(),
        {"1": true()},
    )
    assert str(predicate.compile(compile_kwargs={"literal_binds": True})) == "false"
    rejected(
        lambda: api.query_counterexample(
            catalog,
            rules,
            ctx,
            "user.manage",
            (api.FieldUse(ref, query),),
            column("bad"),
            {"1": true()},
        )
    )
    rejected(
        lambda: api.query_counterexample(
            catalog,
            rules,
            ctx,
            "user.manage",
            (api.FieldUse(ref, query),),
            column("base", Boolean),
            {},
        )
    )
    assert false() is not None


def test_sources_require_current_catalog_same_tenant_and_complete_dependencies(
    api, setup
):
    catalog, ctx, ref, read, *_ = setup
    source = api.SourceRef(
        "tenant", 0, "100", ref, read, catalog.digest, "projection/1"
    )
    node = api.SourceNode("result", (source,), ())
    assert api.collect_sources(
        catalog, (node,), ("result",), tenant_id=0, projection_version="projection/1"
    ) == (source,)
    for invalid_source in (
        replace(source, tenant_id=1),
        replace(source, catalog_digest="0" * 64),
        replace(source, projection_version="projection/2"),
    ):
        rejected(
            lambda invalid_source=invalid_source: api.collect_sources(
                catalog,
                (replace(node, refs=(invalid_source,)),),
                ("result",),
                tenant_id=0,
                projection_version="projection/1",
            )
        )
    rejected(
        lambda: api.collect_sources(
            catalog,
            (replace(node, parents=("result",)),),
            ("result",),
            tenant_id=0,
            projection_version="projection/1",
        )
    )
    rejected(
        lambda: api.collect_sources(
            catalog,
            (replace(node, parents=("missing",)),),
            ("result",),
            tenant_id=0,
            projection_version="projection/1",
        )
    )
    obj = catalog.objects[ref.object_id]
    dependent = replace(obj.fields[1], sources=(api.FieldRef(ref.object_id, "id"),))
    changed = api.Catalog((replace(obj, fields=(obj.fields[0], dependent)),), ())
    new_source = replace(source, catalog_digest=changed.digest)
    rejected(
        lambda: api.collect_sources(
            changed,
            (replace(node, refs=(new_source,)),),
            ("result",),
            tenant_id=0,
            projection_version="projection/1",
        )
    )
    id_source = replace(new_source, ref=api.FieldRef(ref.object_id, "id"))
    assert (
        len(
            api.collect_sources(
                changed,
                (replace(node, refs=(new_source, id_source)),),
                ("result",),
                tenant_id=0,
                projection_version="projection/1",
            )
        )
        == 2
    )
