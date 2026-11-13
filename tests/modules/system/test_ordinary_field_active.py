"""Synthetic qualification exercises active adapters, never opens production admission."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy import FieldRef, Grant
from app.core.field_policy.documents import catalog_parts, rules_document
from app.core.field_policy.qualification import Qualification
from app.db.base import user_roles
from app.modules.system.field_policy.ordinary_catalog import (
    READ,
    SURFACES,
    production_catalog,
)
from app.modules.system.models.data_scope_demo import DataScopeDemo
from app.modules.system.models.dept import Dept
from app.modules.system.models.dict_data import DictData
from app.modules.system.models.dict_type import DictType
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.data_scope_demo import DataScopeDemoQuery
from app.modules.system.schemas.dict_type import DictTypeQuery
from app.modules.system.service.generic_field_policy_migration import migrate_legacy
from tests.modules.system.test_authorization_business_owner import business as business
from tests.modules.system.test_authorization_business_owner import make_type
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_generic_policy_migration import fence
from tests.modules.system.test_generic_policy_persistence import run
from tests.modules.system.test_generic_policy_transitions import command


async def activate(system_sessions, monkeypatch, *, missing=(), extra_grants=()):
    factory, plain = system_sessions
    catalog = production_catalog()
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=catalog,
            catalog_revision="test/ordinary",
            source_build="b" * 40,
        )
        await db.commit()
    policies = factory, plain, catalog
    version = 1
    for object_id, _ in SURFACES.values():
        grants = tuple(
            Grant(0, "201", ref, a)
            for ref, f in catalog.fields.items()
            if ref.object_id == object_id
            for a in f.accesses
            if (ref.field_id, a.operation) not in missing
            and (object_id, ref.field_id, a.operation) not in missing
        )
        rules = rules_document(
            catalog, grants, tenant_id=0, role_id=201, object_id=object_id
        )
        await run(
            policies,
            command(
                "publish",
                version,
                role_id=201,
                object_id=object_id,
                binding_version=0,
                rules=rules,
            ),
        )
        version += 1
    if extra_grants:
        object_id = extra_grants[0].ref.object_id
        role_id = int(extra_grants[0].role_id)
        await run(
            policies,
            command(
                "publish",
                version,
                role_id=role_id,
                object_id=object_id,
                binding_version=0,
                rules=rules_document(
                    catalog,
                    extra_grants,
                    tenant_id=0,
                    role_id=role_id,
                    object_id=object_id,
                ),
            ),
        )
        version += 1
    proof = Qualification(
        "b" * 40,
        catalog.digest,
        *catalog_parts(catalog),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=10),
    )
    built = await run(policies, command("build", version))
    ready = await run(
        policies,
        command("ready", version + 1, expected_epoch=built["epoch"]),
        qualification=proof,
    )
    await run(
        policies,
        command("activate", version + 2, expected_epoch=ready["epoch"]),
        qualification=proof,
    )
    return policies


async def test_demo_field_grant_cannot_cross_its_own_role_scope(
    business, system_sessions, monkeypatch
):
    _, plain, owner = business
    async with plain() as db, db.begin():
        await db.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="demo", ancestors="0", status="1"
            )
        )
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=901,
                user_name="other",
                hashed_password="unused",
                status="1",
            )
        )
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=501,
                role_name="Self fields",
                role_code="SELF",
                data_scope="5",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=501)
        )
        for key, creator in ((701, 101), (702, 901)):
            await db.execute(
                insert(DataScopeDemo).values(
                    tenant_id=0,
                    demo_id=key,
                    dept_id=401,
                    create_by=creator,
                    title="record",
                    content="secret",
                    status="1",
                )
            )
    await activate(
        system_sessions,
        monkeypatch,
        missing=(("content", "read"),),
        extra_grants=(
            Grant(0, "501", FieldRef("system.data_scope_demo", "content"), READ),
        ),
    )
    page = await owner.read(
        tenant=TENANT,
        actor_user_version=1,
        operation="data-scope-demo.list",
        query=DataScopeDemoQuery(),
    )
    assert page["total"] == 2
    own, other = page["records"]
    assert own["content"] == "secret" and "content" not in other


async def test_hidden_value_absent_and_query_denied_before_filtering(
    business, system_sessions, monkeypatch
):
    execute, _, owner = business
    await make_type(execute)
    await activate(
        system_sessions,
        monkeypatch,
        missing=(("dict_name", "read"), ("dict_name", "contains")),
    )
    page = await owner.read(
        tenant=TENANT,
        actor_user_version=1,
        operation="dict-type.list",
        query=DictTypeQuery(),
    )
    assert "dict_name" not in page["records"][0]
    assert "dict_name" not in page["field_policy"]["read"]
    assert "dict_name" not in page["field_policy"]["query"]
    with pytest.raises(BusinessException) as error:
        await owner.read(
            tenant=TENANT,
            actor_user_version=1,
            operation="dict-type.list",
            query=DictTypeQuery(dict_name="no-match"),
        )
    assert error.value.error_code == "FIELD_POLICY_DENIED"
    with pytest.raises(BusinessException):
        await owner.read(
            tenant=TENANT, actor_user_version=1, operation="dict-type.options"
        )


async def test_missing_default_creation_access_cannot_be_omitted(
    business, system_sessions, monkeypatch
):
    execute, plain, _ = business
    await activate(system_sessions, monkeypatch, missing=(("remark", "create"),))
    with pytest.raises(BusinessException) as error:
        await make_type(execute)
    assert error.value.error_code == "FIELD_POLICY_DENIED"
    async with plain() as db:
        assert await db.scalar(select(DictType.dict_type_id)) is None


async def test_applicable_deny_removes_column_capability(
    business, system_sessions, monkeypatch
):
    execute, plain, owner = business
    await make_type(execute)
    async with plain() as db, db.begin():
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=501,
                role_name="Restricted",
                role_code="RESTRICTED",
                data_scope="1",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=501)
        )
    await activate(
        system_sessions,
        monkeypatch,
        extra_grants=(
            Grant(0, "501", FieldRef("system.dict_type", "dict_name"), READ, "deny"),
        ),
    )
    page = await owner.read(
        tenant=TENANT,
        actor_user_version=1,
        operation="dict-type.list",
        query=DictTypeQuery(),
    )
    assert "dict_name" not in page["records"][0]
    assert "dict_name" not in page["field_policy"]["read"]


async def test_preparing_manifest_never_grants_business_reads(business):
    execute, plain, owner = business
    await make_type(execute)
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=production_catalog(),
            catalog_revision="test/ordinary",
            source_build="b" * 40,
        )
        await db.commit()
    with pytest.raises(BusinessException) as error:
        await owner.read(
            tenant=TENANT,
            actor_user_version=1,
            operation="dict-type.list",
            query=DictTypeQuery(),
        )
    assert error.value.error_code == "FIELD_POLICY_UNAVAILABLE"


async def test_parent_lookup_requires_query_access_before_existence_check(
    business, system_sessions, monkeypatch
):
    execute, _, _ = business
    await make_type(execute)
    await activate(
        system_sessions,
        monkeypatch,
        missing=(("system.dict_type", "dict_type", "exact"),),
    )
    for code in ("test_type", "absent"):
        with pytest.raises(BusinessException) as error:
            await execute(
                "dict-data.create",
                payload={
                    "dict_type": code,
                    "dict_label": "label",
                    "dict_value": "value",
                    "dict_sort": 1,
                    "is_default": "N",
                    "status": "1",
                },
            )
        assert error.value.error_code == "FIELD_POLICY_DENIED"


async def test_write_permission_precedes_uniqueness_probe(
    business, system_sessions, monkeypatch
):
    execute, _, _ = business
    one = await make_type(execute, "first")
    await make_type(execute, "second")
    await activate(system_sessions, monkeypatch, missing=(("dict_name", "update"),))
    with pytest.raises(BusinessException) as error:
        await execute(
            "dict-type.update",
            ids=(one["dict_type_id"],),
            payload={"dict_name": "second"},
        )
    assert error.value.error_code == "FIELD_POLICY_DENIED"


async def test_item_pair_and_update_denial_have_no_partial_effect(
    business, system_sessions, monkeypatch
):
    execute, plain, owner = business
    parent = await make_type(execute)
    child = await execute(
        "dict-data.create",
        payload={
            "dict_type": "test_type",
            "dict_label": "label",
            "dict_value": "secret",
            "dict_sort": 1,
            "is_default": "N",
            "status": "1",
        },
    )
    await activate(
        system_sessions,
        monkeypatch,
        missing=(
            ("dict_value", "read"),
            ("dict_label", "update"),
            ("system.dict_data", "dict_type", "update"),
        ),
    )
    with pytest.raises(BusinessException) as error:
        await owner.read(
            tenant=TENANT,
            actor_user_version=1,
            operation="dict-data.options",
            query="test_type",
        )
    assert error.value.error_code == "FIELD_POLICY_DENIED"
    with pytest.raises(BusinessException):
        await execute(
            "dict-data.update",
            ids=(child["dict_code"],),
            payload={"dict_label": "rejected", "status": "2"},
        )
    with pytest.raises(BusinessException):
        await execute(
            "dict-type.update",
            ids=(parent["dict_type_id"],),
            payload={"dict_type": "renamed"},
        )
    async with plain() as db:
        item = await db.get(DictData, child["dict_code"])
        assert (
            item.dict_label == "label"
            and item.status == "1"
            and item.dict_type == "test_type"
        )
        assert (await db.get(DictType, parent["dict_type_id"])).dict_type == "test_type"
