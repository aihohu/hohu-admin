"""应用市场 - 安装/卸载/重装 service（spec 14.4 + 决策 6.4）

状态机：installed → enabled → disabled → uninstalled → installed（循环）
UNIQUE(tenant_id, app_id) 通过「重装 UPDATE 同行」实现循环，避免反复 install/uninstall
产生大量行。

Phase 2 接入低代码：install 时根据 manifest 建 app_data_* 表；
uninstall 时 DROP 表并把表名回填 retained_table_names。
"""

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.modules.marketplace.exceptions import (
    AppInstallLockedException,
    AppNotFoundException,
)
from app.modules.marketplace.lowcode.migration_runner import MigrationRunner
from app.modules.marketplace.lowcode.type_mapping import make_table_name
from app.modules.marketplace.models import App, TenantApp
from app.modules.marketplace.schemas.install import InstallCreate, InstallQuery
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.base import MarketplaceBaseService
from app.modules.marketplace.service.contributes_service import (
    contributes_service,
)
from app.modules.marketplace.service.version_service import version_service
from app.utils.pagination import paginate


class InstallService(MarketplaceBaseService):
    """安装/卸载/重装 service（spec 14.4 + 6.4）"""

    def __init__(self, tenant_id: int = 0):
        super().__init__(tenant_id=tenant_id)
        self.migration_runner = MigrationRunner()

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
            record = await self._do_reinstall(db, existing, version, req)
            await self._create_app_tables(db, app=app, version=version)
            await contributes_service.invalidate(tenant_id=self.tenant_id)
            return record

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
            record = await self._do_reinstall(db, existing, version, req)
            await self._create_app_tables(db, app=app, version=version)
            await contributes_service.invalidate(tenant_id=self.tenant_id)
            return record
        await self._create_app_tables(db, app=app, version=version)
        await contributes_service.invalidate(tenant_id=self.tenant_id)
        return record

    async def _create_app_tables(
        self, db: AsyncSession, *, app: App, version: Any
    ) -> None:
        """根据 manifest 创建 app_data_* 表（spec 6.2）

        - 有 models 数组：每个 model 独立建表 app_data_{slug}_{model_key}
        - 无 models：单表 app_data_{slug}（用顶层 data_schema）
        - manifest 无 data_schema：不建表（纯展示型应用）

        CREATE TABLE IF NOT EXISTS 保证幂等（重装不会重建已有表）。
        """
        manifest = version.manifest or {}

        models = manifest.get("models")
        if models:
            # 多表模式
            for model in models:
                model_key = model.get("key")
                if not model_key:
                    continue
                data_schema = model.get("data_schema") or {}
                if not self._has_user_fields(data_schema):
                    continue
                table_name = make_table_name(app.slug, model_key)
                await self.migration_runner.create_table(
                    db, table_name=table_name, data_schema=data_schema
                )
            return

        # 单表模式
        data_schema = manifest.get("data_schema")
        if self._has_user_fields(data_schema):
            table_name = make_table_name(app.slug)
            await self.migration_runner.create_table(
                db, table_name=table_name, data_schema=data_schema
            )

    @staticmethod
    def _has_user_fields(data_schema: object) -> bool:
        """data_schema 是否含 properties（用于决定是否建表）"""
        return bool(isinstance(data_schema, dict) and data_schema.get("properties"))

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
        """卸载：status='uninstalled'，DROP app_data_* 表并记录 retained_table_names。

        Phase 1 决策（spec 6.4）：默认硬 DROP，软删除留 Phase 2。
        retained_table_names 记录曾存在的表名，供未来重装检测使用。

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

        # 查 app.slug 用于定位应用建的表
        app = await db.get(App, app_id)
        table_names: list[str] = []
        if app is not None:
            table_names = await self.migration_runner.get_table_names_for_app(
                db, app_slug=app.slug
            )
            for tn in table_names:
                await self.migration_runner.drop_table(db, table_name=tn)

        record.status = "uninstalled"
        # 记录曾存在的表（即使为空也写空 list，便于重装时清空）
        record.retained_table_names = table_names
        record.has_data = len(table_names) > 0
        await db.flush()
        await contributes_service.invalidate(tenant_id=self.tenant_id)

    async def enable(self, db: AsyncSession, *, app_id: int) -> TenantApp:
        record = await self._update_status(db, app_id=app_id, status="enabled")
        await contributes_service.invalidate(tenant_id=self.tenant_id)
        return record

    async def disable(self, db: AsyncSession, *, app_id: int) -> TenantApp:
        record = await self._update_status(db, app_id=app_id, status="disabled")
        await contributes_service.invalidate(tenant_id=self.tenant_id)
        return record

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
