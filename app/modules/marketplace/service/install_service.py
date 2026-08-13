"""[LOCAL-ONLY] 安装/卸载/重装 service

操作本地 mk_tenant_app 表 + 调 MigrationRunner 建 app_data_*。
云市场不接触此 service。
如按云端与本地职责拆分，本服务归入 local/install_service.py。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：应用市场 - 安装/卸载/重装 service（spec 14.4 + 决策 6.4）

状态机：installed → enabled → disabled → uninstalled → installed（循环）
UNIQUE(tenant_id, app_id) 通过「重装 UPDATE 同行」实现循环，避免反复 install/uninstall
产生大量行。

低代码安装会根据 manifest 创建 app_data_* 表；
uninstall 时 DROP 表并把表名回填 retained_table_names。
"""

from typing import Any

from sqlalchemy import func, select
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
        """根据 manifest 创建或升级 app_data_* 表（spec 6.2 + 6.4）

        - 有 models 数组：每个 model 独立建表 app_data_{slug}_{model_key}
        - 无 models：单表 app_data_{slug}（用顶层 data_schema）
        - manifest 无 data_schema：不建表（纯展示型应用）

        走 apply_upgrade 而非 create_table：
        - 新装（表不存在）→ apply_upgrade 内部退化为 create_table
        - 重装（表已存在）→ introspect + compare + ALTER TABLE
          这样新版本 manifest 增加的字段会被 ADD COLUMN，widening 的
          VARCHAR 会被 ALTER COLUMN TYPE，避免 v1→v2 升级时丢字段。
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
                await self.migration_runner.apply_upgrade(
                    db, table_name=table_name, new_data_schema=data_schema
                )
            return

        # 单表模式
        data_schema = manifest.get("data_schema")
        if self._has_user_fields(data_schema):
            table_name = make_table_name(app.slug)
            await self.migration_runner.apply_upgrade(
                db, table_name=table_name, new_data_schema=data_schema
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

        当前卸载默认硬 DROP；如引入软删除，需要同时调整重装检测。
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
        # onupdate=func.now() fires server-side on updated_at; refresh to load
        # the new value so Pydantic model_validate doesn't trigger lazy-load
        # (which would raise MissingGreenlet in async context).
        await db.refresh(record)
        return record

    async def list_installed(self, db: AsyncSession, query: InstallQuery) -> PageResult:
        """分页查询已安装应用，联表 App 返回 app_slug / app_name。

        支持 status 和 app_slug 过滤。
        """
        stmt = (
            select(TenantApp, App)
            .join(App, App.id == TenantApp.app_id)
            .where(TenantApp.tenant_id == self.tenant_id)
        )
        if query.status:
            stmt = stmt.where(TenantApp.status == query.status)
        if query.app_slug:
            stmt = stmt.where(App.slug == query.app_slug)

        # 总数
        count_stmt = (
            select(func.count())
            .select_from(TenantApp)
            .join(App, App.id == TenantApp.app_id)
            .where(TenantApp.tenant_id == self.tenant_id)
        )
        if query.status:
            count_stmt = count_stmt.where(TenantApp.status == query.status)
        if query.app_slug:
            count_stmt = count_stmt.where(App.slug == query.app_slug)
        total = (await db.execute(count_stmt)).scalar_one()

        # 分页
        result = await db.execute(
            stmt.order_by(TenantApp.installed_at.desc())
            .offset((query.current - 1) * query.size)
            .limit(query.size)
        )
        records = []
        for tenant_app, app in result:
            records.append(
                {
                    "id": tenant_app.id,
                    "app_id": app.id,
                    "app_slug": app.slug,
                    "app_name": app.name,
                    "installed_version": tenant_app.installed_version,
                    "status": tenant_app.status,
                    "config": tenant_app.config,
                    "installed_at": tenant_app.installed_at,
                    "updated_at": tenant_app.updated_at,
                }
            )

        return PageResult(
            records=records, total=total, current=query.current, size=query.size
        )


install_service = InstallService()
