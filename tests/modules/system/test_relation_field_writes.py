"""Relation writers authorize actual fields before preparing or mutating facts."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy import Grant
from app.core.field_policy.contracts import primitive
from app.core.field_policy.documents import (
    catalog_document,
    catalog_parts,
    digest,
    rules_document,
)
from app.core.field_policy.qualification import Qualification
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as relation_catalog,
)
from app.modules.system.models.dept import Dept
from app.modules.system.models.field_policy import (
    FieldCatalogObject,
    FieldCatalogSnapshot,
    FieldPolicyHead,
    FieldPolicyObjectState,
)
from app.modules.system.models.role import Role
from app.modules.system.schemas.dept import DeptCreate
from app.modules.system.schemas.menu import MenuUpdate
from app.modules.system.schemas.role import RoleUpdate
from app.modules.system.schemas.user import UserPatch
from tests.modules.system.test_authorization_system_candidate import (
    epoch,
    run,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as _system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as _plan_sessions,
)
from tests.modules.system.test_generic_policy_persistence import run as policy_command
from tests.modules.system.test_generic_policy_transitions import command

system_sessions = _system_sessions
plan_sessions = _plan_sessions


async def activate_relations(
    sessions, monkeypatch, *, missing=(), extra_grants=(), catalog=None
):
    factory, plain = sessions
    catalog = catalog or relation_catalog()
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    async with plain() as db:
        action, adapter = catalog_parts(catalog)
        await db.execute(
            insert(FieldCatalogSnapshot).values(
                catalog_digest=catalog.digest,
                schema_version=2,
                catalog_revision="test/relations",
                action_catalog_digest=action,
                adapter_digest=adapter,
                canonical_document=catalog_document(catalog),
            )
        )
        await db.execute(
            insert(FieldPolicyHead).values(tenant_id=0, catalog_digest=catalog.digest)
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
            await db.execute(
                insert(FieldPolicyObjectState).values(
                    tenant_id=0, object_id=obj.object_id, catalog_digest=catalog.digest
                )
            )
        await db.commit()
    policies = factory, plain, catalog
    version = 1
    for object_id in catalog.objects:
        grants = tuple(
            Grant(0, "201", ref, a)
            for ref, field in catalog.fields.items()
            if ref.object_id == object_id
            for a in field.accesses
            if (object_id, ref.field_id, a.operation) not in missing
        )
        await policy_command(
            policies,
            command(
                "publish",
                version,
                role_id=201,
                object_id=object_id,
                binding_version=0,
                rules=rules_document(
                    catalog, grants, tenant_id=0, role_id=201, object_id=object_id
                ),
            ),
        )
        version += 1
    if extra_grants:
        ref = extra_grants[0].ref
        role_id = int(extra_grants[0].role_id)
        await policy_command(
            policies,
            command(
                "publish",
                version,
                role_id=role_id,
                object_id=ref.object_id,
                binding_version=0,
                rules=rules_document(
                    catalog,
                    extra_grants,
                    tenant_id=0,
                    role_id=role_id,
                    object_id=ref.object_id,
                ),
            ),
        )
        version += 1
    proof = Qualification(
        "b" * 40,
        catalog.digest,
        *catalog_parts(catalog),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=20),
    )
    built = await policy_command(policies, command("build", version))
    marked = await policy_command(
        policies,
        command("ready", version + 1, expected_epoch=built["epoch"]),
        qualification=proof,
    )
    await policy_command(
        policies,
        command("activate", version + 2, expected_epoch=marked["epoch"]),
        qualification=proof,
    )


def test_menu_patch_is_sparse_and_closed():
    assert MenuUpdate(icon="new").model_dump(exclude_unset=True) == {"icon": "new"}
    for payload in (
        {},
        {"permission": "new"},
        {"menuName": None},
        {"icon": "x", "roleIds": []},
    ):
        with pytest.raises(ValidationError):
            MenuUpdate.model_validate(payload)


def test_dept_source_id_cannot_be_supplied_by_client():
    with pytest.raises(ValidationError):
        DeptCreate(dept_name="team", status="1", leader_user_id=102)


def test_dept_has_internal_source_fk():
    assert "leader_user_id" in Dept.__table__.c
    assert any(
        fk.name == "fk_sys_dept_tenant_leader"
        for fk in Dept.__table__.foreign_key_constraints
    )


async def test_missing_role_update_field_is_denied_before_write(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.role", "role_desc", "update"),)
    )
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await run(db, "role.update", RoleUpdate(role_desc="hidden"), target_id=201)
        assert error.value.error_code == "FIELD_POLICY_DENIED"
        await db.rollback()
    async with plain() as db:
        assert (
            await db.scalar(select(Role.role_desc).where(Role.role_id == 201)) is None
        )


async def test_last_enabled_manager_cannot_be_disabled(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        before = await epoch(db)
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await run(db, "user.update", UserPatch(status="2"), target_id=101)
        assert error.value.code == 409
        assert error.value.error_code == "AUTHORIZATION_LAST_MANAGER_REQUIRED"
        await db.rollback()
    async with plain() as db:
        assert await epoch(db) == before


async def test_leader_is_persisted_from_verified_match(system_sessions):
    factory, plain = system_sessions
    async with factory() as db:
        result = await run(
            db,
            "dept.create",
            DeptCreate(dept_name="team", status="1", leader="planner"),
        )
        await db.commit()
    async with plain() as db:
        row = await db.get(Dept, result["id"])
        assert row.leader == "planner" and row.leader_user_id == 101
