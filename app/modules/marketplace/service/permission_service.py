"""应用权限声明 service（spec 14.5）。

detail_hash + detail_canonical 在写入时计算，detail_canonical 用于
未来 Hash 算法迁移时回填（参考 git SHA1→SHA256 迁移）。

注意：AppPermission 表无 tenant_id 列（通过 app_id 关联 App 间接归属租户），
因此本 service 不继承 MarketplaceBaseService（其 scoped() 假设 model 有 tenant_id）。
权限查询通过 app_id 关联，自动跟随所属 App 的 tenant。
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.marketplace.models import AppPermission
from app.utils.permission_hash import compute_detail_hash


class PermissionService:
    """应用权限声明 service（spec 14.5）

    AppPermission 表通过 app_id FK 隐式继承 tenant（无独立 tenant_id 列），
    因此不继承 MarketplaceBaseService（其 scoped() 假设 model 有 tenant_id）。
    权限查询通过 app_id 关联，自动跟随所属 App 的 tenant。

    bulk_insert 使用 PG ON CONFLICT DO NOTHING 按 (app_id, type, detail_hash)
    唯一约束去重，保证应用升级时不会产生重复权限声明。
    """

    async def bulk_insert(
        self,
        db: AsyncSession,
        *,
        app_id: int,
        permissions: list[dict],
    ) -> int:
        """批量插入权限声明，重复（按 detail_hash）跳过。

        Args:
            db: 数据库会话
            app_id: 应用 ID
            permissions: manifest permissions 数组，每项 {type, detail}

        Returns:
            实际新增的权限数量
        """
        if not permissions:
            return 0

        rows = []
        for p in permissions:
            detail = p["detail"]
            detail_hash, detail_canonical = compute_detail_hash(detail)
            rows.append(
                {
                    "app_id": app_id,
                    "type": p["type"],
                    "detail": detail,
                    "detail_hash": detail_hash,
                    "detail_canonical": detail_canonical,
                }
            )

        # PG ON CONFLICT DO NOTHING（按 unique constraint uq_mk_app_permission_app_type_hash）
        stmt = (
            pg_insert(AppPermission)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_mk_app_permission_app_type_hash")
            .returning(AppPermission.id)
        )
        result = await db.execute(stmt)
        inserted = result.fetchall()
        await db.flush()
        return len(inserted)

    async def list_by_app(
        self, db: AsyncSession, *, app_id: int
    ) -> list[AppPermission]:
        """列出应用的全部权限声明，按 type、detail_hash 排序（稳定展示）。"""
        stmt = (
            select(AppPermission)
            .where(AppPermission.app_id == app_id)
            .order_by(AppPermission.type, AppPermission.detail_hash)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


permission_service = PermissionService()
