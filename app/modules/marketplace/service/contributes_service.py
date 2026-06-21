"""聚合 contributes 到 Redis 缓存（spec 5.2）

启用 / 禁用应用时，后端聚合所有活跃应用的 contributes（menu、pages、buttons）
为一份扁平 JSON 缓存，按 tenant_id 分桶。前端初始化时一次性加载。
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.modules.marketplace.models import App, AppVersion, TenantApp

CACHE_KEY_PATTERN = "contributes:tenant:{tenant_id}"
CACHE_TTL_SECONDS = 3600


class ContributesService:
    """聚合租户所有 enabled 应用的 contributes，写入 Redis。"""

    async def aggregate_for_tenant(self, db: AsyncSession, *, tenant_id: int) -> dict:
        """聚合某租户的所有 enabled 应用的 contributes"""
        stmt = (
            select(App, AppVersion)
            .join(TenantApp, TenantApp.app_id == App.id)
            .join(AppVersion, AppVersion.id == App.current_version_id)
            .where(
                TenantApp.tenant_id == tenant_id,
                TenantApp.status == "enabled",
            )
            .order_by(TenantApp.installed_at)
        )
        result = await db.execute(stmt)

        menus: list[dict] = []
        pages: list[dict] = []
        for app, version in result:
            manifest = version.manifest or {}
            menu = manifest.get("menu")
            if menu:
                menus.append(
                    {
                        "app_slug": app.slug,
                        "app_name": app.name,
                        "title": menu.get("title", app.name),
                        "icon": menu.get("icon"),
                        "parent": menu.get("parent"),
                        "order": menu.get("order", 100),
                    }
                )

            for page in manifest.get("pages") or []:
                pages.append(
                    {
                        "app_slug": app.slug,
                        "key": page["key"],
                        "title": page.get("title", page["key"]),
                        "page_type": page.get("page_type", "table"),
                    }
                )

        return {"menus": menus, "pages": pages}

    async def refresh_cache(self, db: AsyncSession, *, tenant_id: int) -> dict:
        """重新聚合并写 Redis 缓存"""
        data = await self.aggregate_for_tenant(db, tenant_id=tenant_id)
        redis = await get_redis()
        await redis.set(
            CACHE_KEY_PATTERN.format(tenant_id=tenant_id),
            json.dumps(data, ensure_ascii=False),
            ex=CACHE_TTL_SECONDS,
        )
        return data

    async def invalidate(self, *, tenant_id: int) -> None:
        """删除缓存"""
        redis = await get_redis()
        await redis.delete(CACHE_KEY_PATTERN.format(tenant_id=tenant_id))

    async def get_cached(self, *, tenant_id: int) -> dict | None:
        """读缓存，不存在返回 None"""
        redis = await get_redis()
        cached = await redis.get(CACHE_KEY_PATTERN.format(tenant_id=tenant_id))
        if cached is None:
            return None
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        return json.loads(cached)


contributes_service = ContributesService()
