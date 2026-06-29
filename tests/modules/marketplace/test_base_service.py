from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import next_id
from app.modules.marketplace.models import App
from app.modules.marketplace.service.base import MarketplaceBaseService


class TestMarketplaceBaseService:
    async def test_scoped_filters_by_tenant_id(self, db_session: AsyncSession):
        """_scoped 必须自动加 WHERE tenant_id = ?"""
        app1 = App(
            id=next_id(),
            tenant_id=42,
            name="A",
            slug="a-tenant42",
            type="lowcode",
            category="business",
            status="published",
        )
        app2 = App(
            id=next_id(),
            tenant_id=99,
            name="B",
            slug="b-tenant99",
            type="lowcode",
            category="business",
            status="published",
        )
        db_session.add(app1)
        db_session.add(app2)
        await db_session.flush()

        service = MarketplaceBaseService(tenant_id=42)
        stmt = service.scoped(App)
        result = await db_session.execute(stmt)
        apps = result.scalars().all()
        assert len(apps) == 1
        assert apps[0].tenant_id == 42

    async def test_default_tenant_is_zero(self):
        """Phase 1 默认 tenant_id=0"""
        service = MarketplaceBaseService()
        assert service.tenant_id == 0
