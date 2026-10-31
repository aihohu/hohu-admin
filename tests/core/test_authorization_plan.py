"""Pure complete-plan validation and fixed, real-table catalog contracts."""

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from sqlalchemy import PrimaryKeyConstraint, Table, UniqueConstraint

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import BusinessException
from app.modules.system.service.authorization_resource_catalog import (
    key_values,
    resource_order,
    resource_spec,
)


def invalid(factory, code="FIELD_POLICY_LOCK_ORDER", status=409):
    with pytest.raises(BusinessException) as error:
        factory()
    assert error.value.error_code == code
    assert error.value.code == status


def test_modes_tenant_zero_and_budget_are_typed_immutable_values():
    assert LockMode.SHARED.value == "shared"
    assert LockMode.EXCLUSIVE.value == "exclusive"
    assert TenantLock(0).mode is LockMode.SHARED
    assert TransactionBudget() == TransactionBudget(15000, 2000, 5000)
    assert TransactionBudget(60000, 2000, 5000).timeout_ms == 60000
    with pytest.raises(FrozenInstanceError):
        TenantLock(0).tenant_id = 1
    with pytest.raises(FrozenInstanceError):
        TransactionBudget().timeout_ms = 1


@pytest.mark.parametrize("tenant", [True, False, -1, 2**63, "1", 1.0, None])
def test_tenant_lock_rejects_coercion_and_bigint_overflow(tenant):
    invalid(lambda: TenantLock(tenant))


@pytest.mark.parametrize("mode", ["shared", "exclusive", None, True, 1])
def test_tenant_mode_requires_enum(mode):
    invalid(lambda: TenantLock(0, mode))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        (field, bad)
        for field in ("timeout_ms", "lock_timeout_ms", "statement_timeout_ms")
        for bad in (0, -1, True, "100", 1.5, None)
    ]
    + [
        ("timeout_ms", 60001),
        ("lock_timeout_ms", 2001),
        ("statement_timeout_ms", 5001),
    ],
)
def test_budget_rejects_invalid_values(field, bad):
    invalid(lambda: TransactionBudget(**{field: bad}))


def test_identity_is_canonical_and_global_scope_is_not_tenant_zero():
    key = ResourceKey(ResourceKind.USER, 0, 21)
    assert key == ResourceKey(ResourceKind.USER, 0, (21,))
    assert key_values(key) == (21,)
    assert ResourceKey(ResourceKind.TENANT, None, 0).tenant_id is None
    assert key_values(ResourceKey(ResourceKind.ROLE_AGENT, 0, (21, 22))) == (21, 22)
    assert key_values(
        ResourceKey(ResourceKind.SETTING, 0, "user_require_primary_dept")
    ) == ("user_require_primary_dept",)
    with pytest.raises(FrozenInstanceError):
        key.identity = 22


@pytest.mark.parametrize(
    ("kind", "tenant", "identity"),
    [
        ("user", 0, 1),
        (None, 0, 1),
        (ResourceKind.USER, None, 1),
        (ResourceKind.USER, True, 1),
        (ResourceKind.USER, 2**63, 1),
        (ResourceKind.USER, 0, True),
        (ResourceKind.USER, 0, 0),
        (ResourceKind.USER, 0, -1),
        (ResourceKind.USER, 0, 2**63),
        (ResourceKind.USER, 0, "1"),
        (ResourceKind.USER, 0, (1, 2)),
        (ResourceKind.USER, 0, [1]),
        (ResourceKind.USER, 0, ()),
        (ResourceKind.PROVIDER, 0, 1),
        (ResourceKind.TENANT, 0, 1),
        (ResourceKind.MAIL_CONFIG, None, 2),
        (ResourceKind.ROLE_AGENT, 0, 1),
        (ResourceKind.ROLE_AGENT, 0, (1, "2")),
        (ResourceKind.IMPORT_BATCH, 0, 1),
        (ResourceKind.IMPORT_BATCH, 0, ""),
        (ResourceKind.IMPORT_BATCH, 0, "x" * 65),
        (ResourceKind.IMPORT_BATCH, 0, "has space"),
        (ResourceKind.IMPORT_BATCH, 0, "x\n"),
        (ResourceKind.IMPORT_BATCH, 0, "租户"),
        (ResourceKind.SETTING, 0, "x" * 101),
    ],
)
def test_resource_keys_reject_invalid_scope_shape_and_identity(kind, tenant, identity):
    invalid(lambda: ResourceKey(kind, tenant, identity))


def test_complete_plan_sorts_by_tier_before_tenant_and_keeps_new_separate():
    keys = (
        ResourceKey(ResourceKind.USER, 0, 1),
        ResourceKey(ResourceKind.ROLE, 2, 3),
        ResourceKey(ResourceKind.ROLE, 0, 8),
        ResourceKey(ResourceKind.DEPT, 1, 2),
    )
    new = (
        NewResource(ResourceKey(ResourceKind.USER, 2, 40)),
        NewResource(ResourceKey(ResourceKind.USER, 1, 41)),
    )
    plan = LockPlan(keys, new)
    assert plan.existing == (keys[2], keys[1], keys[3], keys[0])
    assert plan.new == (new[1], new[0])
    assert plan.existing == tuple(sorted(keys, key=resource_order))
    with pytest.raises(FrozenInstanceError):
        plan.existing = ()


def test_conflicting_physical_message_rows_and_duplicate_keys_are_rejected():
    source = ResourceKey(ResourceKind.SOURCE_MESSAGE, 0, 8)
    assistant = ResourceKey(ResourceKind.ASSISTANT_MESSAGE, 0, 8)
    invalid(lambda: LockPlan((source, source)))
    invalid(lambda: LockPlan((source, assistant)))
    invalid(lambda: LockPlan((source,), (NewResource(assistant),)))
    invalid(lambda: LockPlan(new=(NewResource(source), NewResource(source))))
    invalid(lambda: LockPlan(existing=[source]))
    invalid(lambda: LockPlan(new=(source,)))
    invalid(lambda: NewResource("user"))


def test_same_identity_different_tenant_is_never_conflated():
    first = ResourceKey(ResourceKind.USER, 0, 1)
    second = ResourceKey(ResourceKind.USER, 1, 1)
    assert LockPlan((second, first)).existing == (first, second)


@pytest.mark.parametrize(
    ("kind", "limit"),
    [(ResourceKind.ROLE, 4096), (ResourceKind.DEPT, 8192), (ResourceKind.USER, 10000)],
)
def test_domain_limits_count_complete_existing_and_new_union(kind, limit):
    existing = tuple(ResourceKey(kind, 0, i) for i in range(1, limit + 1))
    assert len(LockPlan(existing).existing) == limit
    invalid(
        lambda: LockPlan(existing, (NewResource(ResourceKey(kind, 1, limit + 1)),)),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
        422,
    )


def test_context_limit_counts_all_context_tiers_together():
    keys = tuple(ResourceKey(ResourceKind.CONVERSATION, 0, i) for i in range(1, 16385))
    assert len(LockPlan(keys).existing) == 16384
    invalid(
        lambda: LockPlan(keys + (ResourceKey(ResourceKind.OPERATION_LOG, 0, 1),)),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
        422,
    )


def test_binding_limit_is_per_tenant_and_includes_reserved_rows():
    keys = tuple(
        ResourceKey(ResourceKind.POLICY_BINDING, tenant, role)
        for tenant in (0, 1)
        for role in range(1, 1025)
    )
    assert len(LockPlan(keys).existing) == 2048
    invalid(
        lambda: LockPlan(
            keys, (NewResource(ResourceKey(ResourceKind.POLICY_BINDING, 0, 1025)),)
        ),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
        422,
    )


def test_model_policy_limit_does_not_inherit_the_ordinary_request_cap():
    keys = tuple(
        ResourceKey(ResourceKind.TENANT_MODEL_POLICY, 0, i) for i in range(1, 10001)
    )
    assert len(LockPlan(keys).existing) == 10000
    invalid(
        lambda: LockPlan(
            keys + (ResourceKey(ResourceKind.TENANT_MODEL_POLICY, 0, 10001),)
        ),
        "AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
        422,
    )


def test_fixed_catalog_is_real_complete_and_preserves_insert_defaults():
    assert {kind.name for kind in ResourceKind} == {
        "CONVERSATION",
        "SOURCE_MESSAGE",
        "PREPARED_ACTION",
        "OPERATION_LOG",
        "ROLE",
        "DEPT",
        "USER",
        "TENANT",
        "PLATFORM_PRINCIPAL",
        "MENU",
        "PROVIDER",
        "MODEL",
        "AGENT",
        "TENANT_MODEL_POLICY",
        "ROLE_AGENT",
        "SETTING",
        "CONFIG",
        "MAIL_CONFIG",
        "POLICY_BINDING",
        "POLICY_REVISION",
        "POLICY_AUDIT",
        "IMPORT_BATCH",
        "EXPORT_TASK",
        "IMPORT_BATCH_LOG",
        "ASSISTANT_MESSAGE",
        "ROUTING_LOG",
        "ROUTING_FEEDBACK",
        "USER_ROLES",
        "USER_DEPTS",
        "ROLE_DEPTS",
        "ROLE_MENUS",
        "SYSTEM_OPERATION_LOG",
        "PLATFORM_AUDIT",
        "LOGIN_LOG",
        "UNRESOLVED_LOGIN_LOG",
        "JOB",
        "JOB_LOG",
        "DICT_TYPE",
        "DICT_DATA",
        "DATA_SCOPE_DEMO",
        "FILE",
    }
    for kind in ResourceKind:
        spec = resource_spec(kind)
        assert isinstance(spec.table, Table)
        assert all(name in spec.table.c for name in spec.key_columns)
        if spec.tenant_column:
            assert spec.tenant_column in spec.table.c
            assert spec.tenant_column not in spec.key_columns
        scoped_key = (
            (spec.tenant_column, *spec.key_columns)
            if spec.tenant_column
            else spec.key_columns
        )
        unique_keys = {
            tuple(constraint.columns.keys())
            for constraint in spec.table.constraints
            if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
        }
        # A globally unique PK also makes the tenant-qualified lookup unique.
        assert any(
            columns and set(columns).issubset(scoped_key) for columns in unique_keys
        ), (kind, scoped_key)
        with pytest.raises(FrozenInstanceError):
            spec.order = -1
    user = resource_spec(ResourceKind.USER).table
    assert user.c.user_id.default is not None
    assert "hashed_password" in user.c
    assert resource_spec(ResourceKind.SETTING).key_columns == ("setting_key",)
    assert resource_spec(ResourceKind.ROLE_AGENT).key_columns == ("role_id", "agent_id")
    assert resource_spec(ResourceKind.TENANT_MODEL_POLICY).key_columns == ("model_id",)
    assert resource_spec(ResourceKind.POLICY_BINDING).key_columns == ("role_id",)
    assert resource_spec(ResourceKind.TENANT).tenant_column is None
    assert (
        resource_spec(ResourceKind.SOURCE_MESSAGE).table
        is resource_spec(ResourceKind.ASSISTANT_MESSAGE).table
    )


def test_catalog_rejects_arbitrary_names_and_nonkeys():
    invalid(lambda: resource_spec("sys_user"))
    invalid(lambda: key_values({"kind": "user", "identity": 1}))
    invalid(lambda: resource_order("user"))


def test_pure_plan_import_does_not_import_business_services_or_models():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.core.authorization_plan; "
                "assert not any(name.startswith('app.modules.') for name in sys.modules)"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
