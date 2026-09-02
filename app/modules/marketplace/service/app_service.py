"""[CLOUD-ONLY] App 表 service

操作云市场 mk_app 表。本地 HoHu 不创建此表，浏览时通过 cloud_sync 拉取。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：应用主表 service（spec 14.1）。

提供 CRUD、分页列表和关键词搜索；zhparser 不可用时使用 ILIKE。
所有查询都强制带 tenant_id 过滤（通过 MarketplaceBaseService.scoped）。
"""

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.tenant import TenantContext
from app.modules.marketplace.exceptions import (
    AppDuplicateSlugException,
    AppNotFoundException,
)
from app.modules.marketplace.models import App
from app.modules.marketplace.schemas.app import AppQuery
from app.modules.marketplace.service.base import MarketplaceBaseService
from app.utils.pagination import build_filters, paginate


class AppService(MarketplaceBaseService):
    """应用主表 service"""

    async def get_by_slug(
        self, db: AsyncSession, *, slug: str, tenant: TenantContext
    ) -> App:
        stmt = self.scoped(App, tenant=tenant).where(App.slug == slug)
        result = await db.execute(stmt)
        app = result.scalar_one_or_none()
        if app is None:
            raise AppNotFoundException(slug=slug)
        return app

    async def get_by_id(
        self, db: AsyncSession, *, app_id: int, tenant: TenantContext
    ) -> App:
        stmt = self.scoped(App, tenant=tenant).where(App.id == app_id)
        result = await db.execute(stmt)
        app = result.scalar_one_or_none()
        if app is None:
            raise AppNotFoundException(app_id=app_id)
        return app

    async def create(
        self,
        db: AsyncSession,
        *,
        name: str,
        slug: str,
        type: str,
        category: str,
        description: str | None = None,
        author_id: int | None = None,
        author_name: str | None = None,
        homepage: str | None = None,
        license: str | None = None,
        status: str = "draft",
        tenant: TenantContext,
    ) -> App:
        # slug 唯一性预检（friendly fast-path；并发兜底靠 DB UNIQUE + IntegrityError 翻译）
        existing = await db.execute(
            self.scoped(App, tenant=tenant).where(App.slug == slug)
        )
        if existing.scalar_one_or_none() is not None:
            raise AppDuplicateSlugException(slug)

        app = App(
            tenant_id=tenant.tenant_id,
            name=name,
            slug=slug,
            type=type,
            category=category,
            description=description,
            author_id=author_id,
            author_name=author_name,
            homepage=homepage,
            license=license,
            status=status,
        )
        db.add(app)
        try:
            await db.flush()  # 拿 id；并发时可能触发 UNIQUE 冲突
        except IntegrityError as e:
            # 检查是否是 slug 唯一约束冲突（PG unique constraint name = mk_app_slug_key）
            if "mk_app_slug_key" in str(e.orig) or "slug" in str(e.orig).lower():
                raise AppDuplicateSlugException(slug) from e
            raise  # 其他 IntegrityError 重新抛出
        return app

    async def list(
        self, db: AsyncSession, query: AppQuery, *, tenant: TenantContext
    ) -> PageResult:
        """分页列表 + 筛选（category/status）。

        注意：paginate 不会自动加 tenant_id 过滤，这里手动补上。
        keyword 不在 list 中处理，由 search() 承担。
        """
        field_mapping = {
            "category": ("category", "=="),
            "status": ("status", "=="),
        }
        kwargs = query.model_dump(
            exclude_none=True, exclude={"current", "size", "keyword", "sort"}
        )
        filters = build_filters(App, field_mapping, **kwargs)
        # 强制 tenant_id 过滤（与 MarketplaceBaseService 决策一致）
        self.scoped(App, tenant=tenant)
        filters.append(App.tenant_id == tenant.tenant_id)
        order_by = self._resolve_sort(query.sort)
        return await paginate(db, App, query, filters=filters, order_by=order_by)

    async def search(
        self,
        db: AsyncSession,
        *,
        keyword: str,
        current: int = 1,
        size: int = 10,
        tenant: TenantContext,
    ) -> PageResult:
        """在 zhparser 不可用时使用 ILIKE 搜索。

        仅搜索已发布应用，按下载量倒序。
        """
        pattern = f"%{keyword}%"
        base_filters = [
            App.tenant_id == tenant.tenant_id,
            App.status == "published",
            or_(
                App.name.ilike(pattern),
                App.description.ilike(pattern),
                App.tags_text.ilike(pattern),
            ),
        ]
        self.scoped(App, tenant=tenant)
        # 计数
        count_stmt = select(func.count()).select_from(App).where(*base_filters)
        total = (await db.execute(count_stmt)).scalar() or 0
        # 分页查询
        stmt = (
            select(App)
            .where(*base_filters)
            .order_by(App.download_count.desc())
            .offset((current - 1) * size)
            .limit(size)
        )
        result = await db.execute(stmt)
        records = result.scalars().all()
        return PageResult(
            records=list(records), total=total, current=current, size=size
        )

    def _resolve_sort(self, sort: str):
        return {
            "download": App.download_count.desc(),
            "latest": App.created_at.desc(),
            "rating": App.avg_rating.desc(),
        }.get(sort, App.download_count.desc())


app_service = AppService()
