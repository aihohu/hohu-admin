import pytest
from sqlalchemy import text

from app.core.exceptions import InvalidParameterException, NotFoundException
from app.modules.marketplace.lowcode.data_api_service import DataApiService
from app.modules.marketplace.lowcode.migration_runner import MigrationRunner


@pytest.fixture
async def setup_app_table(db_session):
    """准备一个 app_data_* 表"""
    runner = MigrationRunner()
    table_name = "app_data_dataapi_test"
    await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    await runner.create_table(
        db_session,
        table_name=table_name,
        data_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 100, "default": ""},
                "level": {"type": "string", "default": "C"},
            },
            "required": ["name", "level"],
        },
    )
    await db_session.flush()
    try:
        yield table_name
    finally:
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


class TestDataApiServiceCreate:
    async def test_create_record(self, db_session, setup_app_table):
        svc = DataApiService()
        record = await svc.create(
            db_session,
            table_name=setup_app_table,
            data={"name": "客户A", "level": "A"},
            tenant_id=0,
            user_id=1,
        )
        await db_session.flush()
        assert record["id"] is not None
        assert record["name"] == "客户A"
        assert record["level"] == "A"
        assert record["tenant_id"] == 0

    async def test_create_validates_required(self, db_session, setup_app_table):
        svc = DataApiService()
        with pytest.raises(InvalidParameterException):
            await svc.create(
                db_session,
                table_name=setup_app_table,
                data={"name": "X"},  # 缺 level
                tenant_id=0,
                user_id=1,
                data_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "level": {"type": "string"},
                    },
                    "required": ["name", "level"],
                },
            )


class TestDataApiServiceList:
    async def test_list_paginated(self, db_session, setup_app_table):
        svc = DataApiService()
        for i in range(15):
            await svc.create(
                db_session,
                table_name=setup_app_table,
                data={"name": f"客户{i}", "level": "A"},
                tenant_id=0,
                user_id=1,
            )
        await db_session.flush()

        result = await svc.list(
            db_session,
            table_name=setup_app_table,
            current=1,
            size=10,
            filters=None,
            tenant_id=0,
        )
        assert len(result.records) == 10
        assert result.total == 15

    async def test_list_filters_by_tenant(self, db_session, setup_app_table):
        svc = DataApiService()
        for i in range(5):
            await svc.create(
                db_session,
                table_name=setup_app_table,
                data={"name": f"t0-{i}", "level": "A"},
                tenant_id=0,
                user_id=1,
            )
            await svc.create(
                db_session,
                table_name=setup_app_table,
                data={"name": f"t99-{i}", "level": "A"},
                tenant_id=99,
                user_id=1,
            )
        await db_session.flush()

        result = await svc.list(
            db_session,
            table_name=setup_app_table,
            current=1,
            size=100,
            filters=None,
            tenant_id=0,
        )
        assert result.total == 5
        assert all("t0" in r["name"] for r in result.records)


class TestDataApiServiceGet:
    async def test_get_record(self, db_session, setup_app_table):
        svc = DataApiService()
        record = await svc.create(
            db_session,
            table_name=setup_app_table,
            data={"name": "X", "level": "A"},
            tenant_id=0,
            user_id=1,
        )
        await db_session.flush()

        fetched = await svc.get(
            db_session,
            table_name=setup_app_table,
            record_id=record["id"],
            tenant_id=0,
        )
        assert fetched["name"] == "X"

    async def test_get_not_found(self, db_session, setup_app_table):
        svc = DataApiService()
        with pytest.raises(NotFoundException):
            await svc.get(
                db_session,
                table_name=setup_app_table,
                record_id=99999,
                tenant_id=0,
            )


class TestDataApiServiceUpdate:
    async def test_update_record(self, db_session, setup_app_table):
        svc = DataApiService()
        record = await svc.create(
            db_session,
            table_name=setup_app_table,
            data={"name": "原", "level": "A"},
            tenant_id=0,
            user_id=1,
        )
        await db_session.flush()

        updated = await svc.update(
            db_session,
            table_name=setup_app_table,
            record_id=record["id"],
            data={"name": "改"},
            tenant_id=0,
            user_id=1,
        )
        await db_session.flush()
        assert updated["name"] == "改"

    async def test_update_strips_system_fields(self, db_session, setup_app_table):
        svc = DataApiService()
        record = await svc.create(
            db_session,
            table_name=setup_app_table,
            data={"name": "X", "level": "A"},
            tenant_id=0,
            user_id=1,
        )
        await db_session.flush()

        # 尝试改 tenant_id（应被忽略）
        updated = await svc.update(
            db_session,
            table_name=setup_app_table,
            record_id=record["id"],
            data={"name": "Y", "tenant_id": 999},
            tenant_id=0,
            user_id=1,
        )
        await db_session.flush()
        assert updated["tenant_id"] == 0  # 没被改


class TestDataApiServiceDelete:
    async def test_delete_record(self, db_session, setup_app_table):
        svc = DataApiService()
        record = await svc.create(
            db_session,
            table_name=setup_app_table,
            data={"name": "X", "level": "A"},
            tenant_id=0,
            user_id=1,
        )
        await db_session.flush()

        await svc.delete(
            db_session,
            table_name=setup_app_table,
            record_id=record["id"],
            tenant_id=0,
        )
        await db_session.flush()

        with pytest.raises(NotFoundException):
            await svc.get(
                db_session,
                table_name=setup_app_table,
                record_id=record["id"],
                tenant_id=0,
            )
