from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.modules.marketplace.models import App
from app.modules.marketplace.service.base import MarketplaceBaseService


class TestMarketplaceBaseService:
    async def test_scoped_filters_by_tenant_id(self, db_session: AsyncSession):
        """_scoped 必须自动加 WHERE tenant_id = ?"""
        app1 = App(
            id=next_id(),
            tenant_id=0,
            name="A",
            slug="a-default",
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

        tenant = TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=1,
            tenant_version=1,
            source="access_token",
        )
        service = MarketplaceBaseService()
        stmt = service.scoped(App, tenant=tenant)
        result = await db_session.execute(stmt)
        apps = result.scalars().all()
        assert len(apps) == 1
        assert apps[0].tenant_id == 0

    async def test_service_does_not_keep_mutable_tenant_state(self):
        service = MarketplaceBaseService()
        assert not hasattr(service, "tenant_id")
