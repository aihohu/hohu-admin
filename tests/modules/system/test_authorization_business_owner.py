"""Business facts retain tenant, parent and current-authority constraints."""

from dataclasses import replace

import pytest
from sqlalchemy import insert, select, update

from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_menus, user_depts, user_roles
from app.modules.system.models.data_scope_demo import DataScopeDemo
from app.modules.system.models.dept import Dept
from app.modules.system.models.dict_data import DictData
from app.modules.system.models.dict_type import DictType
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from tests.modules.system.test_authorization_system_candidate import TENANT, epoch
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
def business(system_sessions):
    _, plain = system_sessions
    owner = AuthorizationRuntime(plain.kw["bind"]).business

    async def execute(operation, **kwargs):
        return await owner.execute(
            tenant=TENANT, actor_user_version=1, operation=operation, **kwargs
        )

    return execute, plain, owner


async def make_type(execute, code="test_type"):
    return await execute(
        "dict-type.create",
        payload={"dict_name": code, "dict_type": code, "status": "1"},
    )


async def test_dictionary_parent_delete_is_atomic_and_epoch_unchanged(business):
    execute, plain, _ = business
    parent = await make_type(execute)
    sibling = await make_type(execute, "empty_type")
    data = await execute(
        "dict-data.create",
        payload={
            "dict_type": "test_type",
            "dict_label": "label",
            "dict_value": "value",
            "dict_sort": 1,
            "is_default": "N",
            "status": "1",
        },
    )
    await execute(
        "dict-data.update", ids=(data["dict_code"],), payload={"dict_label": "edited"}
    )
    await execute(
        "dict-type.update", ids=(parent["dict_type_id"],), payload={"remark": "edited"}
    )
    with pytest.raises(BusinessException):
        await execute(
            "dict-type.batch-delete",
            ids=(parent["dict_type_id"], sibling["dict_type_id"]),
        )
    async with plain() as db:
        assert await db.get(DictType, sibling["dict_type_id"]) is not None
        assert (await db.get(DictData, data["dict_code"])).dict_label == "edited"
        assert await epoch(db) == 1
    assert await execute("dict-data.batch-delete", ids=(data["dict_code"],)) == 1
    assert (
        await execute(
            "dict-type.batch-delete",
            ids=(parent["dict_type_id"], sibling["dict_type_id"]),
        )
        == 2
    )


async def test_missing_dictionary_parent_and_mixed_batch_do_not_partially_write(
    business,
):
    execute, plain, _ = business
    parent = await make_type(execute)
    with pytest.raises(BusinessException):
        await execute(
            "dict-data.create",
            payload={
                "dict_type": "absent",
                "dict_label": "x",
                "dict_value": "x",
                "dict_sort": 1,
                "is_default": "N",
                "status": "1",
            },
        )
    with pytest.raises(BusinessException):
        await execute("dict-type.batch-delete", ids=(parent["dict_type_id"], 999))
    async with plain() as db:
        assert await db.get(DictType, parent["dict_type_id"]) is not None
        assert await db.scalar(select(DictData.dict_code)) is None


async def test_cross_tenant_dictionary_id_is_not_a_target(business):
    execute, plain, _ = business
    async with plain() as db, db.begin():
        await db.execute(
            insert(DictType).values(
                tenant_id=1,
                dict_type_id=800,
                dict_name="foreign",
                dict_type="foreign",
                status="1",
            )
        )
    with pytest.raises(BusinessException) as error:
        await execute("dict-type.delete", ids=(800,))
    assert error.value.code == 404
    async with plain() as db:
        assert (await db.get(DictType, 800)).tenant_id == 1


async def test_stale_original_user_version_cannot_create_business_facts(business):
    execute, plain, _ = business
    async with plain() as db, db.begin():
        await db.execute(update(User).where(User.user_id == 101).values(auth_version=2))
    with pytest.raises(BusinessException) as error:
        await make_type(execute)
    assert error.value.error_code == "INVALID_CREDENTIALS"
    async with plain() as db:
        assert await db.scalar(select(DictType.dict_type_id)) is None
        assert await epoch(db) == 1


async def test_demo_uses_primary_department_and_current_data_scope(business):
    execute, plain, owner = business
    async with plain() as db, db.begin():
        await db.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="Primary", ancestors="0", status="1"
            )
        )
        await db.execute(
            insert(user_depts).values(
                tenant_id=0, user_id=101, dept_id=401, is_primary="Y"
            )
        )
    row = await execute("data-scope-demo.create", payload={"title": "owned"})
    assert (row["dept_id"], row["create_by"]) == (401, 101)
    changed = await execute(
        "data-scope-demo.update", ids=(row["demo_id"],), payload={"content": "changed"}
    )
    assert changed["content"] == "changed"
    async with plain() as db, db.begin():
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=1801,
                user_name="other",
                hashed_password="unused",
                status="1",
            )
        )
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=1802,
                role_name="Self only",
                role_code="SELF",
                data_scope="5",
                status="1",
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9999,
                menu_name="Delete demo",
                menu_type="F",
                status="1",
                permission="system:data-scope-demo:delete",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=1802, menu_id=9999)
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=1801, role_id=1802)
        )
    with pytest.raises(BusinessException) as error:
        await owner.execute(
            tenant=replace(TENANT, actor_user_id=1801),
            actor_user_version=1,
            operation="data-scope-demo.delete",
            ids=(row["demo_id"],),
        )
    assert error.value.code == 404
    async with plain() as db:
        assert await db.get(DataScopeDemo, row["demo_id"]) is not None
    assert await execute("data-scope-demo.delete", ids=(row["demo_id"],)) == 1
    async with plain() as db:
        assert await epoch(db) == 1


async def test_business_limits_and_revoked_role_fail_before_mutation(business):
    execute, plain, _ = business
    with pytest.raises(BusinessException):
        await execute("dict-type.batch-delete", ids=tuple(range(1, 258)))
    with pytest.raises(BusinessException) as error:
        await execute(
            "data-scope-demo.create",
            payload={"title": "x", "content": "x" * (256 * 1024 + 1)},
        )
    assert error.value.error_code == "BUSINESS_INPUT_TOO_LARGE"
    async with plain() as db, db.begin():
        await db.execute(update(Role).where(Role.role_id == 201).values(status="2"))
    with pytest.raises(BusinessException):
        await make_type(execute)
    async with plain() as db:
        assert await db.scalar(select(DictType.dict_type_id)) is None
