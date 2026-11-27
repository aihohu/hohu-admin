"""Multi-object, multi-tenant immutable reuse with real sealed manifests."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, text, update

from app.core.authorization_plan import LockMode, TenantLock
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy import Access, FieldRef, Grant
from app.core.field_policy.contracts import primitive
from app.core.field_policy.documents import (
    catalog_document,
    catalog_parts,
    digest,
    rules_document,
)
from app.core.field_policy.qualification import Qualification
from app.core.tenant_scope import TenantContext
from app.modules.system.field_policy.ordinary_catalog import relation_catalog
from app.modules.system.models.field_policy import (
    FieldCatalogObject,
    FieldCatalogSnapshot,
    FieldPolicyHead,
    FieldPolicyObjectState,
)
from app.modules.system.service import generic_field_policy_service as module
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as _plan_sessions,
)
from tests.modules.system.test_generic_policy_persistence import publish, run
from tests.modules.system.test_generic_policy_transitions import command, transactions
from tests.modules.system.test_relation_field_snapshot import (  # noqa: F401
    generic_sessions as _generic_sessions,
)

generic_sessions = _generic_sessions


async def test_workers_recheck_revocation_and_recovery(relation_policies):
    _, plain, catalog = relation_policies
    async with plain() as db:
        schema = await db.scalar(text("select current_schema()"))
    workers = []

    async def start():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.modules.system.snapshot_worker",
            schema,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        workers.append(process)
        return process

    async def read(process, tenant=0):
        process.stdin.write(f"{tenant}\n".encode())
        await process.stdin.drain()
        return json.loads(await asyncio.wait_for(process.stdout.readline(), 45))

    try:
        for _ in range(2):
            worker = await start()
            assert await read(worker) == [["role_name"], 1]
            assert await read(worker) == [["role_name"], 0]
        await run(
            relation_policies,
            publish(version=6) | {"object_id": "system.role", "binding_version": 1},
        )
        built = await run(relation_policies, command("build", 7))
        proof = Qualification(
            "b" * 40,
            catalog.digest,
            *catalog_parts(catalog),
            "c" * 64,
            datetime.now(UTC) + timedelta(minutes=10),
        )
        ready = await run(
            relation_policies,
            command("ready", 8, expected_epoch=built["epoch"]),
            qualification=proof,
        )
        await run(
            relation_policies,
            command("activate", 9, expected_epoch=ready["epoch"]),
            qualification=proof,
        )
        for worker in workers:
            assert await read(worker) == [[], 1]
            assert await read(worker, 1) == [["role_name"], 1]
        async with plain() as db:
            await db.execute(
                update(FieldPolicyObjectState)
                .where(
                    FieldPolicyObjectState.tenant_id == 0,
                    FieldPolicyObjectState.object_id == "system.role",
                )
                .values(mode="frozen")
            )
            await db.commit()
        for worker in workers:
            assert await read(worker) == ["FIELD_POLICY_UNAVAILABLE", 0]
            worker.stdin.close()
            assert await asyncio.wait_for(worker.wait(), 30) == 0
        async with plain() as db:
            await db.execute(
                update(FieldPolicyObjectState)
                .where(
                    FieldPolicyObjectState.tenant_id == 0,
                    FieldPolicyObjectState.object_id == "system.role",
                )
                .values(mode="enforcing")
            )
            await db.commit()
        assert await read(await start()) == [[], 1]
    finally:
        for worker in workers:
            if worker.returncode is None:
                worker.kill()
            await worker.wait()


plan_sessions = _plan_sessions


@pytest.fixture
async def relation_policies(generic_sessions, monkeypatch):
    factory, plain = generic_sessions
    catalog = relation_catalog()
    action, adapter = catalog_parts(catalog)
    async with plain() as db:
        await db.execute(
            insert(FieldCatalogSnapshot).values(
                catalog_digest=catalog.digest,
                schema_version=2,
                catalog_revision="test/relation",
                action_catalog_digest=action,
                adapter_digest=adapter,
                canonical_document=catalog_document(catalog),
            )
        )
        for obj in catalog.objects.values():
            await db.execute(
                insert(FieldCatalogObject).values(
                    catalog_digest=catalog.digest,
                    object_id=obj.object_id,
                    domain_kind="tenant",
                    schema_digest=digest(primitive(obj)),
                    adapter_version=obj.scope_adapter,
                )
            )
        for tenant_id in (0, 1):
            await db.execute(
                insert(FieldPolicyHead).values(
                    tenant_id=tenant_id, catalog_digest=catalog.digest
                )
            )
            for obj in catalog.objects:
                await db.execute(
                    insert(FieldPolicyObjectState).values(
                        tenant_id=tenant_id,
                        object_id=obj,
                        catalog_digest=catalog.digest,
                    )
                )
        await db.commit()
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    proof = Qualification(
        "b" * 40,
        catalog.digest,
        action,
        adapter,
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=30),
    )
    policies = factory, plain, catalog
    for tenant_id, role_id in ((0, 201), (1, 202)):
        for version, (obj, field) in enumerate(
            (("system.user", "roles"), ("system.role", "role_name")), 1
        ):
            grant = Grant(
                tenant_id,
                str(role_id),
                FieldRef(obj, field),
                Access("read", "management.read"),
            )
            await run(
                policies,
                publish(version=version, role_id=role_id)
                | {
                    "object_id": obj,
                    "rules": rules_document(
                        catalog,
                        (grant,),
                        tenant_id=tenant_id,
                        role_id=role_id,
                        object_id=obj,
                    ),
                },
                tenant_id=tenant_id,
            )
        built = await run(policies, command("build", 3), tenant_id=tenant_id)
        marked = await run(
            policies,
            command("ready", 4, expected_epoch=built["epoch"]),
            tenant_id=tenant_id,
            qualification=proof,
        )
        await run(
            policies,
            command("activate", 5, expected_epoch=marked["epoch"]),
            tenant_id=tenant_id,
            qualification=proof,
        )
    return policies


async def test_shared_manifest_never_shares_object_or_role_results(
    relation_policies, monkeypatch
):
    factory, _, catalog = relation_policies
    spy = AsyncMock(wraps=module.read_manifest)
    monkeypatch.setattr(module, "read_manifest", spy)
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(0), TenantLock(1))) as tx:
            for tenant_id, role_id in ((0, "201"), (1, "202")):
                for obj in ("system.user", "system.role"):
                    grants = await module.generic_field_policy_service.load_active(
                        db,
                        tx,
                        tenant_id=tenant_id,
                        catalog=catalog,
                        object_id=obj,
                        role_ids=(role_id,),
                    )
                    assert (
                        len(grants) == 1
                        and grants[0].ref.object_id == obj
                        and grants[0].tenant_id == tenant_id
                    )
                    assert (
                        await module.generic_field_policy_service.load_active(
                            db,
                            tx,
                            tenant_id=tenant_id,
                            catalog=catalog,
                            object_id=obj,
                            role_ids=("999",),
                        )
                        == ()
                    )
            assert spy.await_count == 2
            again = await module.generic_field_policy_service.load_active(
                db,
                tx,
                tenant_id=0,
                catalog=catalog,
                object_id="system.role",
                role_ids=("201",),
            )
            assert again[0].tenant_id == 0 and spy.await_count == 3
        await db.rollback()


@pytest.mark.parametrize(
    "change", [{"mode": "frozen"}, {"active_object_digest": "f" * 64}]
)
async def test_batch_checks_every_requested_dependency(relation_policies, change):
    factory, plain, catalog = relation_policies
    async with plain() as db:
        await db.execute(
            update(FieldPolicyObjectState)
            .where(
                FieldPolicyObjectState.tenant_id == 0,
                FieldPolicyObjectState.object_id == "system.dept",
            )
            .values(**change)
        )
        await db.commit()
    service = module.generic_field_policy_service
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            args = {"tenant_id": 0, "catalog": catalog, "role_ids": ("201",)}
            assert await service.load_active(db, tx, object_id="system.user", **args)
            with pytest.raises(BusinessException) as exc:
                await service.load_active_objects(
                    db,
                    tx,
                    object_ids={"system.role", "system.user", "system.dept"},
                    **args,
                )
            assert exc.value.error_code == "FIELD_POLICY_UNAVAILABLE"
        await db.rollback()


async def test_new_candidate_cannot_replace_cached_active_rules(
    relation_policies, monkeypatch
):
    factory, _, catalog = relation_policies
    service = module.generic_field_policy_service
    spy = AsyncMock(wraps=module.read_manifest)
    monkeypatch.setattr(module, "read_manifest", spy)
    tenant = TenantContext(0, "plan-0", 101, 1, "access_token")
    async with factory() as db:
        async with transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            before = await service.load_active(
                db,
                tx,
                tenant_id=0,
                catalog=catalog,
                object_id="system.role",
                role_ids=("201",),
            )
            plan = await service.plan(
                db,
                tx,
                tenant=tenant,
                catalog=catalog,
                command=publish(version=6)
                | {"object_id": "system.role", "binding_version": 1},
            )
            await tx.lock(plan.lock_plan)
            await service.apply(db, tx, plan, tenant=tenant, catalog=catalog)
            after = await service.load_active(
                db,
                tx,
                tenant_id=0,
                catalog=catalog,
                object_id="system.role",
                role_ids=("201",),
            )
            assert before == after and len(after) == 1 and spy.await_count == 1
        await db.rollback()
