"""[LOCAL-ONLY] 聚合 contributes 到 Redis

聚合本地已 enabled 应用的菜单/页面，前端启动时一次加载。
云市场不接触此 service。
如按云端与本地职责拆分，本服务归入 local/contributes_service.py。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：聚合 contributes 到 Redis 缓存（spec 5.2）

启用 / 禁用应用时，后端聚合所有活跃应用的 contributes（menu、pages、buttons）
为一份扁平 JSON 缓存，按 tenant_id 分桶。前端初始化时一次性加载。
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.core.tenant import TenantContext
from app.modules.marketplace.capability import require_marketplace_capability
from app.modules.marketplace.models import App, AppVersion, TenantApp

CACHE_KEY_PATTERN = "contributes:tenant:{tenant_id}"
CACHE_TTL_SECONDS = 3600


class ContributesService:
    """聚合租户所有 enabled 应用的 contributes，写入 Redis。"""

    async def aggregate_for_tenant(
        self, db: AsyncSession, *, tenant: TenantContext
    ) -> dict:
        """聚合某租户的所有 enabled 应用的 contributes"""
        require_marketplace_capability(tenant)
        stmt = (
            select(App, AppVersion)
            .join(TenantApp, TenantApp.app_id == App.id)
            .join(AppVersion, AppVersion.id == App.current_version_id)
            .where(
                TenantApp.tenant_id == tenant.tenant_id,
                App.tenant_id == tenant.tenant_id,
                TenantApp.status == "enabled",
                AppVersion.app_id == App.id,
            )
            .order_by(TenantApp.installed_at)
        )
        result = await db.execute(stmt)

        menus: list[dict] = []
        pages: list[dict] = []
        for app, version in result:
            manifest = version.manifest or {}

            # Support both manifest.menu (singular, backward compat) and
            # manifest.menus (plural array). Plural takes precedence when both
            # declared (avoids ambiguity, matches relations[] convention).
            raw_menus: list[dict] = []
            menus_arr = manifest.get("menus")
            if isinstance(menus_arr, list):
                raw_menus = [m for m in menus_arr if isinstance(m, dict)]
            elif manifest.get("menu"):
                raw_menus = [manifest.get("menu")]

            for menu in raw_menus:
                menus.append(
                    {
                        "app_slug": app.slug,
                        "app_name": app.name,
                        "title": menu.get("title", app.name),
                        "icon": menu.get("icon"),
                        "parent": menu.get("parent"),
                        "order": menu.get("order", 100),
                        "page_key": menu.get("page_key"),
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

    async def refresh_cache(self, db: AsyncSession, *, tenant: TenantContext) -> dict:
        """重新聚合并写 Redis 缓存"""
        data = await self.aggregate_for_tenant(db, tenant=tenant)
        redis = await get_redis()
        await redis.set(
            CACHE_KEY_PATTERN.format(tenant_id=tenant.tenant_id),
            json.dumps(data, ensure_ascii=False),
            ex=CACHE_TTL_SECONDS,
        )
        return data

    async def invalidate(self, *, tenant: TenantContext) -> None:
        """删除缓存"""
        require_marketplace_capability(tenant)
        redis = await get_redis()
        await redis.delete(CACHE_KEY_PATTERN.format(tenant_id=tenant.tenant_id))

    async def get_cached(self, *, tenant: TenantContext) -> dict | None:
        """读缓存，不存在返回 None"""
        require_marketplace_capability(tenant)
        redis = await get_redis()
        cached = await redis.get(CACHE_KEY_PATTERN.format(tenant_id=tenant.tenant_id))
        if cached is None:
            return None
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        return json.loads(cached)


contributes_service = ContributesService()
