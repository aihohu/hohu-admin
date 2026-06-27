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


@pytest.fixture
async def setup_filter_table(db_session):
    """含 text/integer/jsonb 三类列的表，用于 filter operator 测试"""
    runner = MigrationRunner()
    table_name = "app_data_filter_test"
    await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    await runner.create_table(
        db_session,
        table_name=table_name,
        data_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 100},
                "status": {"type": "string"},
                "age": {"type": "integer"},
                "tags": {"type": "array"},
            },
            "required": ["name"],
        },
    )
    await db_session.flush()
    try:
        yield table_name
    finally:
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


async def _seed_filter_data(svc, db_session, table_name):
    """塞 5 条样本：name/status/age/tags 各异"""
    samples = [
        {"name": "alice", "status": "active", "age": 18, "tags": ["vip", "premium"]},
        {"name": "bob", "status": "active", "age": 30, "tags": ["normal"]},
        {"name": "charlie", "status": "pending", "age": 50, "tags": ["vip"]},
        {"name": "david abc", "status": "pending", "age": 66, "tags": ["normal"]},
        {"name": "eve", "status": "draft", "age": 17, "tags": []},
    ]
    for s in samples:
        await svc.create(
            db_session,
            table_name=table_name,
            data=s,
            tenant_id=0,
            user_id=1,
        )
    await db_session.flush()


class TestDataApiFilters:
    """spec 6.2 filter operators：contains / in / gte / lte / has"""

    async def test_contains_ilike(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"name__contains": "abc"},
            tenant_id=0,
        )
        names = {r["name"] for r in result.records}
        assert names == {"david abc"}

    async def test_contains_partial_match(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"name__contains": "li"},
            tenant_id=0,
        )
        # alice / charlie 都含 'li'，bob/david abc/eve 不含
        names = {r["name"] for r in result.records}
        assert names == {"alice", "charlie"}

    async def test_in_csv(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"status__in": "active,pending"},
            tenant_id=0,
        )
        names = {r["name"] for r in result.records}
        assert names == {"alice", "bob", "charlie", "david abc"}

    async def test_gte(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"age__gte": 30},
            tenant_id=0,
        )
        ages = {r["age"] for r in result.records}
        assert ages == {30, 50, 66}

    async def test_lte(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"age__lte": 30},
            tenant_id=0,
        )
        ages = {r["age"] for r in result.records}
        assert ages == {18, 30, 17}

    async def test_has_jsonb_array(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"tags__has": "vip"},
            tenant_id=0,
        )
        names = {r["name"] for r in result.records}
        assert names == {"alice", "charlie"}

    async def test_exact_no_suffix(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"status": "active"},
            tenant_id=0,
        )
        names = {r["name"] for r in result.records}
        assert names == {"alice", "bob"}

    async def test_combined_and(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={
                "status__in": "active,pending",
                "age__gte": 30,
                "tags__has": "vip",
            },
            tenant_id=0,
        )
        # 仅 charlie 同时满足：status∈{active,pending} + age>=30 + tags has vip
        names = {r["name"] for r in result.records}
        assert names == {"charlie"}

    async def test_gte_with_string_value(self, db_session, setup_filter_table):
        """Query params arrive as strings; numeric gte/lte must CAST to avoid
        PG type error on `integer >= text`."""
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters={"age__gte": "30"},  # string, not int
            tenant_id=0,
        )
        ages = {r["age"] for r in result.records}
        assert ages == {30, 50, 66}

    async def test_unknown_field_rejected(self, db_session, setup_filter_table):
        svc = DataApiService()
        with pytest.raises(InvalidParameterException):
            await svc.list(
                db_session,
                table_name=setup_filter_table,
                current=1,
                size=10,
                filters={"unknown__contains": "x"},
                tenant_id=0,
            )

    async def test_type_mismatch_contains_on_integer(
        self, db_session, setup_filter_table
    ):
        svc = DataApiService()
        with pytest.raises(InvalidParameterException):
            await svc.list(
                db_session,
                table_name=setup_filter_table,
                current=1,
                size=10,
                filters={"age__contains": "x"},
                tenant_id=0,
            )

    async def test_type_mismatch_has_on_text(self, db_session, setup_filter_table):
        svc = DataApiService()
        with pytest.raises(InvalidParameterException):
            await svc.list(
                db_session,
                table_name=setup_filter_table,
                current=1,
                size=10,
                filters={"name__has": "x"},
                tenant_id=0,
            )

    async def test_system_field_rejected(self, db_session, setup_filter_table):
        svc = DataApiService()
        with pytest.raises(InvalidParameterException):
            await svc.list(
                db_session,
                table_name=setup_filter_table,
                current=1,
                size=10,
                filters={"tenant_id__gte": 0},
                tenant_id=0,
            )

    async def test_invalid_operator_rejected(self, db_session, setup_filter_table):
        svc = DataApiService()
        with pytest.raises(InvalidParameterException):
            await svc.list(
                db_session,
                table_name=setup_filter_table,
                current=1,
                size=10,
                filters={"name__bogus": "x"},
                tenant_id=0,
            )


class TestDataApiOrderBy:
    """spec 6.2 order_by：- 前缀表 desc，多列逗号分隔"""

    async def test_order_by_desc(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters=None,
            tenant_id=0,
            order_by="-age",
        )
        ages = [r["age"] for r in result.records]
        assert ages == sorted(ages, reverse=True)

    async def test_order_by_asc_multi(self, db_session, setup_filter_table):
        svc = DataApiService()
        await _seed_filter_data(svc, db_session, setup_filter_table)
        result = await svc.list(
            db_session,
            table_name=setup_filter_table,
            current=1,
            size=100,
            filters=None,
            tenant_id=0,
            order_by="status,age",
        )
        # 按 status asc，同 status 内按 age asc
        records = result.records
        for i in range(1, len(records)):
            prev, cur = records[i - 1], records[i]
            assert (prev["status"], prev["age"]) <= (cur["status"], cur["age"])

    async def test_order_by_unknown_field_rejected(
        self, db_session, setup_filter_table
    ):
        svc = DataApiService()
        with pytest.raises(InvalidParameterException):
            await svc.list(
                db_session,
                table_name=setup_filter_table,
                current=1,
                size=10,
                filters=None,
                tenant_id=0,
                order_by="unknown_field",
            )
