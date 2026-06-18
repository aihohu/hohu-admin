"""应用市场 - 安装/卸载/重装 service（spec 14.4 + 决策 6.4）

状态机：installed → enabled → disabled → uninstalled → installed（循环）
UNIQUE(tenant_id, app_id) 通过「重装 UPDATE 同行」实现循环，避免反复 install/uninstall
产生大量行。

Phase 1 没有低代码表，retained_table_names 永远为空 list；Phase 2 接入低代码后，
卸载时从 information_schema 查询应用建的表，回填 retained_table_names。
"""

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.modules.marketplace.exceptions import (
    AppInstallLockedException,
    AppNotFoundException,
)
from app.modules.marketplace.models import TenantApp
from app.modules.marketplace.schemas.install import InstallCreate, InstallQuery
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.base import MarketplaceBaseService
from app.modules.marketplace.service.version_service import version_service
from app.utils.pagination import paginate


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

        并发兜底：两个并发 install 在「freshly-uninstalled 行」上都会预检返回 None，
        各自 INSERT 后第二个会触发 DB UNIQUE(uq_mk_tenant_app_tenant_app) 冲突，
        此时 rollback + 重查并退化为 UPDATE（保证两请求最终幂等）。
        若重查仍无记录（极少见，例如已被另一个事务删干净），抛 AppInstallLockedException
        让调用方重试。

        Args:
            db: 数据库会话（调用方负责 commit）
            req: 安装请求（app_slug 必填；version=None 表示最新 approved 版本）
            user_id: 操作用户ID

        Raises:
            AppNotFoundException: 应用不存在，或无 approved 版本
            AppInstallLockedException: 并发冲突且重查仍无行可 UPDATE
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
            return await self._do_reinstall(db, existing, version, req)

        # 新装：INSERT — 并发兜底：catch UNIQUE 冲突退化为 UPDATE
        record = TenantApp(
            tenant_id=self.tenant_id,
            app_id=app.id,
            installed_version=version.version,
            status="installed",  # spec 决策 #10：默认 installed
            config=req.config,
            approved_permissions=req.approved_permissions,
        )
        db.add(record)
        try:
            await db.flush()
        except IntegrityError as e:
            if "uq_mk_tenant_app_tenant_app" not in str(e.orig):
                raise
            # 并发场景：另一请求已 INSERT，本请求 rollback 后退化为 UPDATE
            await db.rollback()
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if existing is None:
                # 极少见：rollback 后另一行也消失了（理论不该发生，防御性抛错）
                raise AppInstallLockedException(app.id) from e
            return await self._do_reinstall(db, existing, version, req)
        return record

    async def _do_reinstall(
        self,
        db: AsyncSession,
        existing: TenantApp,
        version: Any,
        req: InstallCreate,
    ) -> TenantApp:
        """重装 UPDATE 同行（spec 6.4 决策）。

        status 重置为 installed，清空 retained_table_names，更新版本/权限/配置。
        """
        existing.status = "installed"
        existing.installed_version = version.version
        existing.approved_permissions = req.approved_permissions
        existing.config = req.config
        # 重装清空 retained_table_names（与 uninstall 保持一致，空 list）
        existing.retained_table_names: list[Any] = []
        existing.has_data = False
        await db.flush()
        return existing

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
        # Phase 1 没建 app_data_* 表，retained_table_names 为空 list
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

    async def list_installed(self, db: AsyncSession, query: InstallQuery) -> PageResult:
        """分页查询已安装应用。

        注意：paginate 不会自动加 tenant_id 过滤，这里手动补上（与 AppService.list 一致）。
        """
        filters = [TenantApp.tenant_id == self.tenant_id]
        if query.status:
            filters.append(TenantApp.status == query.status)
        return await paginate(
            db,
            TenantApp,
            query,
            filters=filters,
            order_by=TenantApp.installed_at.desc(),
        )


install_service = InstallService()
