import pytest

from app.modules.marketplace.exceptions import (
    AppDuplicateSlugException,
    AppNotFoundException,
)
from app.modules.marketplace.models import App
from app.modules.marketplace.schemas.app import AppQuery
from app.modules.marketplace.service.app_service import app_service


class TestAppService:
    async def test_get_by_slug_not_found(self, db_session):
        with pytest.raises(AppNotFoundException):
            await app_service.get_by_slug(db_session, slug="non-existent")

    async def test_get_by_slug_found(self, db_session):
        await app_service.create(
            db_session,
            name="客户管理",
            slug="zhangsan-crm",
            type="lowcode",
            category="business",
            author_name="张三",
        )
        await db_session.flush()

        found = await app_service.get_by_slug(db_session, slug="zhangsan-crm")
        assert found.slug == "zhangsan-crm"
        assert found.tenant_id == 0  # 默认租户

    async def test_get_by_id_not_found(self, db_session):
        with pytest.raises(AppNotFoundException):
            await app_service.get_by_id(db_session, app_id=99999999)

    async def test_get_by_id_found(self, db_session):
        app = await app_service.create(
            db_session,
            name="测试",
            slug="get-by-id-test",
            type="lowcode",
            category="business",
        )
        await db_session.flush()

        found = await app_service.get_by_id(db_session, app_id=app.id)
        assert found.id == app.id

    async def test_duplicate_slug_rejected(self, db_session):
        await app_service.create(
            db_session,
            name="A",
            slug="dup-slug",
            type="lowcode",
            category="business",
        )
        await db_session.flush()
        with pytest.raises(AppDuplicateSlugException):
            await app_service.create(
                db_session,
                name="B",
                slug="dup-slug",
                type="lowcode",
                category="business",
            )

    async def test_list_paginated(self, db_session):
        for i in range(15):
            await app_service.create(
                db_session,
                name=f"App{i}",
                slug=f"list-app-{i}",
                type="lowcode",
                category="business",
                status="published",
            )
        await db_session.flush()

        result = await app_service.list(
            db_session, AppQuery(current=1, size=10, status=None)
        )
        assert len(result.records) == 10
        assert result.total == 15
        assert result.current == 1

    async def test_list_filter_by_status(self, db_session):
        await app_service.create(
            db_session,
            name="A",
            slug="status-a",
            type="lowcode",
            category="business",
            status="published",
        )
        await app_service.create(
            db_session,
            name="B",
            slug="status-b",
            type="lowcode",
            category="business",
            status="draft",
        )
        await db_session.flush()

        result = await app_service.list(db_session, AppQuery(status="published"))
        assert all(r.status == "published" for r in result.records)
        assert result.total == 1

    async def test_list_filter_by_category(self, db_session):
        await app_service.create(
            db_session,
            name="A",
            slug="cat-business",
            type="lowcode",
            category="business",
            status="published",
        )
        await app_service.create(
            db_session,
            name="B",
            slug="cat-dev",
            type="lowcode",
            category="dev",
            status="published",
        )
        await db_session.flush()

        result = await app_service.list(
            db_session, AppQuery(category="dev", status=None)
        )
        assert all(r.category == "dev" for r in result.records)
        assert result.total == 1

    async def test_list_respects_tenant_id(self, db_session):
        """scoped 必须过滤 tenant_id — 其他租户数据不可见"""
        other_tenant_app = App(
            tenant_id=999,
            name="Other",
            slug="other-tenant-only",
            type="lowcode",
            category="business",
            status="published",
        )
        db_session.add(other_tenant_app)
        await app_service.create(
            db_session,
            name="Mine",
            slug="mine-default",
            type="lowcode",
            category="business",
            status="published",
        )
        await db_session.flush()

        result = await app_service.list(db_session, AppQuery(status=None))
        slugs = {r.slug for r in result.records}
        assert "mine-default" in slugs
        assert "other-tenant-only" not in slugs

    async def test_search_by_keyword(self, db_session):
        await app_service.create(
            db_session,
            name="客户管理",
            slug="search-crm",
            type="lowcode",
            category="business",
            status="published",
            description="CRM 系统",
        )
        await app_service.create(
            db_session,
            name="订单管理",
            slug="search-order",
            type="lowcode",
            category="business",
            status="published",
            description="订单系统",
        )
        await db_session.flush()

        result = await app_service.search(
            db_session, keyword="客户", current=1, size=10
        )
        assert any("客户" in r.name for r in result.records)
        assert all(r.status == "published" for r in result.records)

    async def test_search_excludes_draft(self, db_session):
        await app_service.create(
            db_session,
            name="客户管理",
            slug="exclude-draft",
            type="lowcode",
            category="business",
            status="draft",  # 非 published
        )
        await db_session.flush()
        result = await app_service.search(db_session, keyword="客户")
        assert result.total == 0

    async def test_search_paginated(self, db_session):
        for i in range(12):
            await app_service.create(
                db_session,
                name=f"客户{i}",
                slug=f"search-page-{i}",
                type="lowcode",
                category="business",
                status="published",
            )
        await db_session.flush()

        page1 = await app_service.search(db_session, keyword="客户", current=1, size=5)
        page2 = await app_service.search(db_session, keyword="客户", current=2, size=5)
        assert len(page1.records) == 5
        assert len(page2.records) == 5
        assert page1.total == 12
        page1_ids = {r.id for r in page1.records}
        page2_ids = {r.id for r in page2.records}
        assert page1_ids.isdisjoint(page2_ids)  # 两页无重叠

    async def test_sort_by_download(self, db_session):
        a = await app_service.create(
            db_session,
            name="A",
            slug="sort-a",
            type="lowcode",
            category="business",
            status="published",
        )
        b = await app_service.create(
            db_session,
            name="B",
            slug="sort-b",
            type="lowcode",
            category="business",
            status="published",
        )
        a.download_count = 10
        b.download_count = 100
        await db_session.flush()

        result = await app_service.list(
            db_session, AppQuery(sort="download", status=None)
        )
        # B (100) 在前，A (10) 在后
        assert result.records[0].slug == "sort-b"
        assert result.records[1].slug == "sort-a"
