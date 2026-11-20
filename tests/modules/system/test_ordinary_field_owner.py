"""Ordinary reads and writes close the same authorization transaction."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.modules.system.models.dict_data import DictData
from app.modules.system.schemas.dict_type import DictTypeQuery
from tests.modules.system.test_authorization_business_owner import business as business
from tests.modules.system.test_authorization_business_owner import make_type
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_ordinary_list_returns_string_anchor_and_safe_dto(business):
    execute, _, owner = business
    row = await make_type(execute)
    page = await owner.read(
        tenant=TENANT,
        actor_user_version=1,
        operation="dict-type.list",
        query=DictTypeQuery(),
    )
    assert page["total"] == 1
    assert page["records"][0]["dict_type_id"] == str(row["dict_type_id"])
    assert "tenant_id" not in page["records"][0]
    assert "create_by" not in page["records"][0]


async def test_default_item_conflict_is_rejected_without_partial_write(business):
    execute, plain, _ = business
    await make_type(execute)
    payload = {
        "dict_type": "test_type",
        "dict_label": "one",
        "dict_value": "1",
        "dict_sort": 1,
        "is_default": "Y",
        "status": "1",
    }
    await execute("dict-data.create", payload=payload)
    with pytest.raises(BusinessException) as error:
        await execute(
            "dict-data.create",
            payload=payload | {"dict_label": "two", "dict_value": "2"},
        )
    assert error.value.error_code == "DICT_DEFAULT_CONFLICT"
    async with plain() as db:
        assert len((await db.scalars(select(DictData.dict_code))).all()) == 1


async def test_options_require_current_list_permission(business):
    _, _, owner = business
    # tenant 1 actor has no role; logging in alone does not authorize options.
    with pytest.raises(BusinessException):
        await owner.read(
            tenant=replace(
                TENANT, tenant_id=1, tenant_code="plan-1", actor_user_id=102
            ),
            actor_user_version=1,
            operation="dict-type.options",
        )


async def test_type_rename_updates_every_child_atomically(business):
    execute, plain, _ = business
    parent = await make_type(execute)
    child = await execute(
        "dict-data.create",
        payload={
            "dict_type": "test_type",
            "dict_label": "one",
            "dict_value": "1",
            "dict_sort": 1,
            "is_default": "N",
            "status": "1",
        },
    )
    await execute(
        "dict-type.update",
        ids=(parent["dict_type_id"],),
        payload={"dict_type": "renamed_type"},
    )
    async with plain() as db:
        assert (await db.get(DictData, child["dict_code"])).dict_type == "renamed_type"
