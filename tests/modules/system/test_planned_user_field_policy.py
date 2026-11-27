"""Policy candidates consume complete transaction proofs and preserve history."""

from dataclasses import replace
from importlib import import_module

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db.base import Base, role_depts, role_menus
from app.modules.system.field_policy.baseline import (
    RoleBaselineSource,
    build_role_baseline,
)
from app.modules.system.field_policy.documents import (
    canonical_rules,
    catalog_digest,
    document_digest,
    rules_digest,
)
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.user_fields import CATALOG_REVISION
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyAudit as Audit,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyBinding as Binding,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyRevision as Revision,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyState as State,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.planned_user_field_policy_service import (
    assert_role_history_deletable,
)
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)

TENANT = TenantContext(0, "plan-0", 101, 1, "access_token")


@pytest.fixture
async def policy_sessions(plan_sessions):
    _, plain = plan_sessions
    async with plain.kw["bind"].begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Dept.__table__, Menu.__table__, role_depts, role_menus]
            )
        )
    async with plain() as db:
        await db.execute(
            insert(Menu.__table__).values(
                tenant_id=0,
                menu_id=301,
                menu_name="users",
                status="1",
                permission="system:user:list",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=301)
        )
        await db.commit()
    return plan_sessions


async def change(factory, action="publish", **kwargs):
    role_id = kwargs.pop("role_id", 201)
    async with factory() as db:
        async with transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            plan = await policies.plan(
                db,
                tx,
                tenant=TENANT,
                action=action,
                role_id=None if action == "ready" else role_id,
            )
            await tx.lock(plan.lock_plan)
            method = (
                policies.mark_ready if action == "ready" else getattr(policies, action)
            )
            result = await method(
                db,
                tx,
                plan,
                tenant=TENANT,
                reason="candidate review",
                correlation_id="planned-test",
                **kwargs,
            )
        await db.commit()
        return result


def grants():
    return (FieldGrant("201", "nickname", "read", "management.read"),)


async def state(plain):
    async with plain() as db:
        return await db.scalar(select(State).where(State.tenant_id == 0))


async def test_publish_archive_and_republish_use_one_epoch_each(policy_sessions):
    factory, plain = policy_sessions
    first = await change(factory, grants=grants(), expected_version=0)
    assert first.version == 1 and not first.archived
    assert (await state(plain)).epoch == 2
    retired = await change(factory, "archive", expected_version=1)
    assert retired.version == 2 and retired.archived
    assert (await state(plain)).epoch == 3
    third = await change(factory, grants=grants(), expected_version=2)
    assert third.version == 3 and third.revision_no == 3
    current = await state(plain)
    assert current.epoch == 4 and current.mode == "legacy"
    assert current.active_manifest_digest is None and not current.ever_enforced
    async with plain() as db:
        assert len((await db.scalars(select(Revision.revision_id))).all()) == 2
        assert (await db.scalars(select(Audit.epoch).order_by(Audit.epoch))).all() == [
            2,
            3,
            4,
        ]


async def test_ready_manual_candidate_noop_and_stale_epoch(policy_sessions):
    factory, plain = policy_sessions
    await change(factory, grants=grants(), expected_version=0)
    current = await state(plain)
    ready = await change(
        factory,
        "ready",
        expected_epoch=current.epoch,
        expected_manifest_digest=current.candidate_manifest_digest,
    )
    assert ready.mode == "ready" and ready.epoch == current.epoch + 1
    replay = await change(
        factory,
        "ready",
        expected_epoch=ready.epoch,
        expected_manifest_digest=ready.candidate_manifest_digest,
    )
    assert replay == ready
    with pytest.raises(BusinessException) as failure:
        await change(
            factory,
            "ready",
            expected_epoch=current.epoch,
            expected_manifest_digest=current.candidate_manifest_digest,
        )
    assert failure.value.error_code == "FIELD_POLICY_VERSION_CONFLICT"
    async with plain() as db:
        assert (
            await db.scalars(select(Audit.action).order_by(Audit.audit_id))
        ).all() == ["publish", "ready"]


@pytest.mark.parametrize(
    "mutation", ["role_code", "status", "data_scope", "permission", "department"]
)
async def test_ready_rejects_changed_legacy_role_source(policy_sessions, mutation):
    factory, plain = policy_sessions
    baseline = build_role_baseline(
        RoleBaselineSource(
            0,
            201,
            "PLAN",
            "1",
            frozenset({"system:user:list"}),
            "1",
            (),
        )
    )
    await change(
        factory,
        grants=baseline.rules,
        expected_version=0,
        source_digest=baseline.source_digest,
        template_revision=baseline.template_revision,
    )
    current = await state(plain)
    async with plain() as db:
        if mutation == "permission":
            await db.execute(
                update(Menu)
                .where(Menu.menu_id == 301)
                .values(permission="system:user:edit")
            )
        elif mutation == "department":
            await db.execute(
                insert(Dept.__table__).values(
                    tenant_id=0, dept_id=401, dept_name="source"
                )
            )
            await db.execute(
                insert(role_depts).values(tenant_id=0, role_id=201, dept_id=401)
            )
        else:
            value = {"role_code": "CHANGED", "status": "2", "data_scope": "5"}[mutation]
            await db.execute(
                update(Role).where(Role.role_id == 201).values({mutation: value})
            )
        await db.commit()
    with pytest.raises(BusinessException) as failure:
        await change(
            factory,
            "ready",
            expected_epoch=current.epoch,
            expected_manifest_digest=current.candidate_manifest_digest,
        )
    assert failure.value.error_code == "FIELD_POLICY_STALE"
    after = await state(plain)
    assert after.epoch == current.epoch and after.mode == "legacy"


async def test_valid_legacy_source_can_be_ready(policy_sessions):
    factory, plain = policy_sessions
    baseline = build_role_baseline(
        RoleBaselineSource(
            0,
            201,
            "PLAN",
            "1",
            frozenset({"system:user:list"}),
            "1",
            (),
        )
    )
    await change(
        factory,
        grants=baseline.rules,
        expected_version=0,
        source_digest=baseline.source_digest,
        template_revision=baseline.template_revision,
    )
    current = await state(plain)
    ready = await change(
        factory,
        "ready",
        expected_epoch=current.epoch,
        expected_manifest_digest=current.candidate_manifest_digest,
    )
    assert ready.mode == "ready"


@pytest.mark.parametrize("archived", [False, True])
async def test_unknown_template_only_blocks_live_bindings(policy_sessions, archived):
    factory, plain = policy_sessions
    await change(
        factory,
        grants=grants(),
        expected_version=0,
        source_digest="a" * 64,
        template_revision="unknown/v1",
    )
    if archived:
        await change(factory, "archive", expected_version=1)
    current = await state(plain)
    if archived:
        result = await change(
            factory,
            "ready",
            expected_epoch=current.epoch,
            expected_manifest_digest=current.candidate_manifest_digest,
        )
        assert result.mode == "ready"
    else:
        with pytest.raises(BusinessException) as failure:
            await change(
                factory,
                "ready",
                expected_epoch=current.epoch,
                expected_manifest_digest=current.candidate_manifest_digest,
            )
        assert failure.value.error_code == "FIELD_POLICY_STALE"


@pytest.mark.parametrize("archived", [False, True])
async def test_role_history_helper_rejects_whole_target_set(policy_sessions, archived):
    factory, plain = policy_sessions
    await change(factory, grants=grants(), expected_version=0)
    if archived:
        await change(factory, "archive", expected_version=1)
    async with plain() as db:
        await db.execute(
            insert(Role.__table__).values(
                tenant_id=0,
                role_id=203,
                role_name="empty",
                role_code="EMPTY",
                status="1",
                data_scope="1",
            )
        )
        await db.commit()
    before = await state(plain)
    async with factory() as db:
        with pytest.raises(BusinessException) as failure:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(
                    LockPlan(
                        existing=tuple(
                            ResourceKey(ResourceKind.ROLE, 0, value)
                            for value in (201, 203)
                        )
                    )
                )
                await assert_role_history_deletable(
                    db, tx, tenant=TENANT, role_ids=(203, 201)
                )
        assert failure.value.error_code == "ROLE_DELETE_POLICY_HISTORY_REFERENCED"
        await db.rollback()
    assert (await state(plain)).epoch == before.epoch
    async with plain() as db:
        assert (
            await db.scalars(
                select(Role.role_id).where(Role.tenant_id == 0).order_by(Role.role_id)
            )
        ).all() == [201, 203]
        assert len((await db.scalars(select(Binding.role_id))).all()) == 1


@pytest.mark.parametrize(
    "action,omitted",
    [
        ("publish", ResourceKind.USER),
        ("publish", ResourceKind.ROLE),
        ("ready", ResourceKind.ROLE),
        ("ready", ResourceKind.POLICY_BINDING),
    ],
)
async def test_declared_plan_does_not_replace_actual_required_locks(
    policy_sessions, action, omitted
):
    factory, plain = policy_sessions
    if action == "ready":
        await change(factory, grants=grants(), expected_version=0)
    before = await state(plain)
    async with factory() as db:
        with pytest.raises(BusinessException) as failure:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                plan = await policies.plan(
                    db,
                    tx,
                    tenant=TENANT,
                    action=action,
                    role_id=None if action == "ready" else 201,
                )
                plan = replace(
                    plan,
                    lock_plan=LockPlan(
                        existing=tuple(
                            key
                            for key in plan.lock_plan.existing
                            if key.kind is not omitted
                        ),
                        new=plan.lock_plan.new,
                    ),
                )
                await tx.lock(plan.lock_plan)
                kwargs = (
                    {
                        "expected_epoch": before.epoch,
                        "expected_manifest_digest": before.candidate_manifest_digest,
                    }
                    if action == "ready"
                    else {"grants": grants(), "expected_version": 0}
                )
                method = policies.mark_ready if action == "ready" else policies.publish
                await method(
                    db,
                    tx,
                    plan,
                    tenant=TENANT,
                    reason="proof boundary",
                    correlation_id="declared-not-held",
                    **kwargs,
                )
        assert failure.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        await db.rollback()
    after = await state(plain)
    assert (after.epoch, after.mode, after.candidate_manifest_digest) == (
        before.epoch,
        before.mode,
        before.candidate_manifest_digest,
    )


async def test_audit_insert_failure_rolls_back_revision_binding_and_epoch(
    policy_sessions,
):
    factory, plain = policy_sessions
    await change(factory, grants=grants(), expected_version=0)
    before = await state(plain)
    async with plain() as db:
        duplicate_id = await db.scalar(select(Audit.audit_id))
    async with factory() as db:
        with pytest.raises(IntegrityError):
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                plan = await policies.plan(
                    db, tx, tenant=TENANT, action="publish", role_id=201
                )
                plan = replace(
                    plan,
                    audit_id=duplicate_id,
                    lock_plan=LockPlan(
                        existing=plan.lock_plan.existing,
                        new=tuple(
                            NewResource(
                                ResourceKey(ResourceKind.POLICY_AUDIT, 0, duplicate_id)
                            )
                            if item.key.kind is ResourceKind.POLICY_AUDIT
                            else item
                            for item in plan.lock_plan.new
                        ),
                    ),
                )
                await tx.lock(plan.lock_plan)
                await policies.publish(
                    db,
                    tx,
                    plan,
                    tenant=TENANT,
                    grants=grants(),
                    expected_version=1,
                    reason="duplicate audit",
                    correlation_id="atomic-policy",
                )
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    after = await state(plain)
    assert (after.epoch, after.candidate_manifest_digest) == (
        before.epoch,
        before.candidate_manifest_digest,
    )
    async with plain() as db:
        assert (await db.scalars(select(Binding.version))).all() == [1]
        assert len((await db.scalars(select(Revision.revision_id))).all()) == 1
        assert len((await db.scalars(select(Audit.audit_id))).all()) == 1


@pytest.mark.parametrize("phase", ["plan", "apply"])
@pytest.mark.parametrize("budget_name", ["_SOURCE_MAX_ROWS", "_SOURCE_MAX_BYTES"])
async def test_source_read_budget_rejects_and_poisoned_owner_cannot_commit(
    policy_sessions, monkeypatch, phase, budget_name
):
    factory, plain = policy_sessions
    await change(factory, grants=grants(), expected_version=0)
    async with plain() as setup:
        await setup.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=203,
                role_code="SECOND",
                role_name="second",
                status="1",
                data_scope="1",
            )
        )
        await setup.execute(
            insert(role_menus).values(tenant_id=0, role_id=203, menu_id=301)
        )
        await setup.commit()
    await change(
        factory,
        role_id=203,
        grants=(FieldGrant("203", "nickname", "read", "management.read"),),
        expected_version=0,
    )
    before = await state(plain)
    module = import_module(
        "app.modules.system.service.planned_user_field_policy_service"
    )
    # Each role alone fits; only the accumulated source work exceeds the bound.
    limit = 3 if budget_name == "_SOURCE_MAX_ROWS" else 800
    async with factory() as db:
        with pytest.raises(BusinessException) as failure:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                if phase == "plan":
                    monkeypatch.setattr(module, budget_name, limit, raising=False)
                plan = await policies.plan(db, tx, tenant=TENANT, action="ready")
                await tx.lock(plan.lock_plan)
                if phase == "apply":
                    monkeypatch.setattr(module, budget_name, limit, raising=False)
                await policies.mark_ready(
                    db,
                    tx,
                    plan,
                    tenant=TENANT,
                    expected_epoch=before.epoch,
                    expected_manifest_digest=before.candidate_manifest_digest,
                    reason="source budget",
                    correlation_id="source-budget",
                )
        assert failure.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    after = await state(plain)
    assert (after.epoch, after.mode, after.candidate_manifest_digest) == (
        before.epoch,
        before.mode,
        before.candidate_manifest_digest,
    )


@pytest.mark.parametrize("count", [17, 1025])
async def test_manifest_keyset_pages_and_total_binding_limit(policy_sessions, count):
    factory, plain = policy_sessions
    revisions, bindings, manifest_items = [], [], []
    async with plain() as db:
        roles = [
            {
                "tenant_id": 0,
                "role_id": rid,
                "role_code": f"PAGE_{rid}",
                "role_name": f"page-{rid}",
                "status": "1",
                "data_scope": "1",
            }
            for rid in range(1000, 1000 + count)
        ]
        await db.execute(insert(Role.__table__), roles)
        for offset, rid in enumerate(range(1000, 1000 + count)):
            revision_id = 10000 + offset
            document = canonical_rules(
                (FieldGrant(str(rid), "nickname", "read", "management.read"),),
                role_id=rid,
            )
            digest = rules_digest(document)
            revisions.append(
                {
                    "tenant_id": 0,
                    "role_id": rid,
                    "revision_id": revision_id,
                    "revision_no": 1,
                    "catalog_revision": CATALOG_REVISION,
                    "catalog_digest": catalog_digest(),
                    "rules": document,
                    "rules_digest": digest,
                    "created_by_user_id": 101,
                }
            )
            bindings.append(
                {
                    "tenant_id": 0,
                    "role_id": rid,
                    "revision_id": revision_id,
                    "version": 1,
                }
            )
            manifest_items.append(
                {
                    "role_id": str(rid),
                    "revision_id": str(revision_id),
                    "version": 1,
                    "archived": False,
                    "rules_digest": digest,
                }
            )
        await db.execute(insert(Revision.__table__), revisions)
        await db.execute(insert(Binding.__table__), bindings)
        manifest = document_digest(
            {"catalog_digest": catalog_digest(), "bindings": manifest_items}
        )
        await db.execute(
            update(State)
            .where(State.tenant_id == 0)
            .values(epoch=2, candidate_manifest_digest=manifest)
        )
        await db.commit()
    if count > 1024:
        with pytest.raises(BusinessException) as failure:
            await change(
                factory, "ready", expected_epoch=2, expected_manifest_digest=manifest
            )
        assert failure.value.error_code == "FIELD_POLICY_INVALID"
        assert (await state(plain)).epoch == 2
        return
    manifest_pages = []

    def record(_connection, _cursor, sql, _parameters, context, _many):
        if "JOIN sys_user_field_policy_revision" in sql:
            manifest_pages.append(context.compiled_parameters[0])

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    try:
        result = await change(
            factory, "ready", expected_epoch=2, expected_manifest_digest=manifest
        )
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    assert result.mode == "ready" and result.epoch == 3
    assert len(manifest_pages) == 3  # 16 rows, then 1, then the terminal empty page.
    assert all(parameters["param_1"] == 16 for parameters in manifest_pages)
