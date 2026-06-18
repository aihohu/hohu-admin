"""应用市场 - 安装/卸载/重装 service（spec 14.4 + 决策 6.4）

状态机：installed → enabled → disabled → uninstalled → installed（循环）
UNIQUE(tenant_id, app_id) 通过「重装 UPDATE 同行」实现循环，避免反复 install/uninstall
产生大量行。

Phase 1 没有低代码表，retained_table_names 永远为空 list；Phase 2 接入低代码后，
卸载时从 information_schema 查询应用建的表，回填 retained_table_names。
"""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.marketplace.exceptions import AppNotFoundException
from app.modules.marketplace.models import TenantApp
from app.modules.marketplace.schemas.install import InstallCreate, InstallQuery
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.base import MarketplaceBaseService
from app.modules.marketplace.service.version_service import version_service


class InstallService(MarketplaceBaseService):
    """安装/卸载/重装 service（spec 14.4 + 6.4）"""

    async def install(
        self,
        db: AsyncSession,
        req: InstallCreate,
        *,
        user_id: int,  # noqa: ARG002 - 预留审计字段
    ) -> TenantApp:
        """安装或重装应用。

        - 新装：INSERT 新行，status='installed'
        - 重装：UPDATE 同一行（满足 UNIQUE(tenant_id, app_id)），
          status 重置为 installed，清空 retained_table_names

        Args:
            db: 数据库会话（调用方负责 commit）
            req: 安装请求（app_slug 必填；version=None 表示最新 approved 版本）
            user_id: 操作用户ID

        Raises:
            AppNotFoundException: 应用不存在，或无 approved 版本
        """
        # 查应用
        app = await app_service.get_by_slug(db, slug=req.app_slug)
        # 查版本（默认最新 approved）
        if req.version:
            version = await version_service.get_by_version(
                db, app_id=app.id, version=req.version
            )
        else:
            version = await version_service.get_latest_approved(db, app_id=app.id)
            if version is None:
                raise AppNotFoundException(slug=f"{req.app_slug} (no approved version)")

        # 查 tenant_app（可能存在历史 uninstalled 记录）
        stmt = self.scoped(TenantApp).where(TenantApp.app_id == app.id)
        existing = (await db.execute(stmt)).scalar_one_or_none()

        if existing is not None:
            # 重装：UPDATE 同行（spec 6.4 决策）
            existing.status = "installed"
            existing.installed_version = version.version
            existing.approved_permissions = req.approved_permissions
            existing.config = req.config
            # 重装清空 retained_table_names（应用数据已被接管）
            existing.retained_table_names = None
            existing.has_data = False
            existing.updated_at = datetime.utcnow()
            await db.flush()
            return existing

        # 新装：INSERT
        record = TenantApp(
            tenant_id=self.tenant_id,
            app_id=app.id,
            installed_version=version.version,
            status="installed",  # spec 决策 #10：默认 installed
            config=req.config,
            approved_permissions=req.approved_permissions,
        )
        db.add(record)
        await db.flush()
        return record

    async def uninstall(
        self,
        db: AsyncSession,
        *,
        app_id: int,
        user_id: int,  # noqa: ARG002 - 预留审计字段
    ) -> None:
        """卸载：status='uninstalled'，不删行。

        Phase 1 没建 app_data_* 表，retained_table_names 为空 list。
        Phase 2 接入低代码后会从 information_schema 查询应用建的表。

        Args:
            db: 数据库会话
            app_id: 应用ID
            user_id: 操作用户ID

        Raises:
            AppNotFoundException: 该应用未安装
        """
        stmt = self.scoped(TenantApp).where(TenantApp.app_id == app_id)
        record = (await db.execute(stmt)).scalar_one_or_none()
        if record is None:
            raise AppNotFoundException(app_id=app_id)

        record.status = "uninstalled"
        # Phase 1 没建 app_data_* 表，retained_table_names 为空
        # Phase 2 接入低代码后会从 information_schema 查询应用建的表
        record.retained_table_names: list[Any] = []
        record.has_data = False
        await db.flush()

    async def enable(self, db: AsyncSession, *, app_id: int) -> TenantApp:
        return await self._update_status(db, app_id=app_id, status="enabled")

    async def disable(self, db: AsyncSession, *, app_id: int) -> TenantApp:
        return await self._update_status(db, app_id=app_id, status="disabled")

    async def _update_status(
        self, db: AsyncSession, *, app_id: int, status: str
    ) -> TenantApp:
        stmt = self.scoped(TenantApp).where(TenantApp.app_id == app_id)
        record = (await db.execute(stmt)).scalar_one_or_none()
        if record is None:
            raise AppNotFoundException(app_id=app_id)
        record.status = status
        await db.flush()
        return record

    async def list_installed(
        self, db: AsyncSession, query: InstallQuery
    ) -> list[TenantApp]:
        stmt = self.scoped(TenantApp).order_by(TenantApp.installed_at.desc())
        if query.status:
            stmt = stmt.where(TenantApp.status == query.status)
        result = await db.execute(stmt)
        return list(result.scalars().all())


install_service = InstallService()
