"""Current values classify only after a real replacement and complete guard."""

from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.models.config import Config
from app.modules.system.service.authorization_configuration_candidate import (
    configuration_authorization_candidate as candidate,
)
from app.modules.system.service.control_config_input import CONTROL_IMPORT_HEADERS
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_parameter_read_owner import parameters as parameters
from tests.modules.system.test_parameter_read_owner import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_parameter_read_owner import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def write(factory, operation, payload=None, target=None):
    async with factory() as db:
        result = await candidate.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            payload=payload,
            target_id=target,
        )
        await db.commit()
        return result.value


async def test_real_replacement_classifies_unknown_metadata_and_masks_do_not(
    parameters,
):
    factory, plain = parameters
    await write(factory, "config.update", {"remark": "metadata"}, 302)
    await write(factory, "config.update", {"config_value": "******"}, 302)
    async with plain() as db:
        row = await db.get(Config, 302)
        assert row.value_sensitivity == "unknown" and row.config_value == "value-302"
    result = await write(factory, "config.update", {"config_value": "validated"}, 302)
    assert result == {"config_id": "302"}
    async with plain() as db:
        row = await db.get(Config, 302)
        assert row.value_sensitivity == "private" and row.config_value == "validated"
    await write(
        factory,
        "config.update",
        {"config_key": "business.renamed", "config_value": "replacement"},
        303,
    )
    async with plain() as db:
        assert (await db.get(Config, 303)).value_sensitivity == "secret"


async def test_unreadable_field_may_be_written_only_with_actual_write_grant(
    parameters, monkeypatch
):
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(
            ("system.config", "config_value", "read"),
            ("system.config", "remark", "update"),
        ),
    )
    await write(parameters[0], "config.update", {"config_value": "blind allowed"}, 301)
    with pytest.raises(BusinessException) as denied:
        await write(parameters[0], "config.update", {"remark": "not allowed"}, 301)
    assert denied.value.error_code == "FIELD_POLICY_DENIED"
    async with parameters[1]() as db:
        assert (await db.get(Config, 301)).remark is None


@pytest.mark.parametrize(
    "payload",
    [
        {"config_value": '{"secret":"x"}'},
        {"config_type": "file"},
        {"is_public": True},
        {"value_sensitivity": "private"},
    ],
)
async def test_unregistered_shape_or_direct_publication_never_writes(
    parameters, payload
):
    with pytest.raises(BusinessException):
        await write(parameters[0], "config.update", payload, 302)
    async with parameters[1]() as db:
        assert (
            await db.scalar(select(Config.config_value).where(Config.config_id == 302))
            == "value-302"
        )


async def test_wired_import_uses_strict_closed_parser_and_boolean():

    def workbook(key="business.example", public="false"):
        book = Workbook()
        book.active.append(CONTROL_IMPORT_HEADERS)
        book.active.append(["Example", key, "text", "text", "group", "1", public, None])
        buffer = BytesIO()
        book.save(buffer)
        book.close()
        return buffer.getvalue()

    prepared = await candidate.parse_import(workbook())
    assert dict(prepared.records[0])["is_public"] is False
    for payload in (
        workbook(public="no"),
        workbook(public=True),
        workbook(key="security:login_per_minute"),
    ):
        with pytest.raises(BusinessException):
            await candidate.parse_import(payload)
