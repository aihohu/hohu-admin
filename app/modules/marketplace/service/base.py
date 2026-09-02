"""[SHARED] MarketplaceBaseService

云市场与本地 HoHu 都用此基类（共享 tenant_id 过滤逻辑）。
按云端与本地职责拆分时，本基类仍保留原位。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：所有 marketplace Service 必须继承，强制 tenant_id 过滤。

决策 #1：所有应用数据相关查询必须带 WHERE tenant_id = ?
单租户模式（默认 0）也必须显式传入，避免未来升级多租户时遗漏过滤导致越权。
"""

from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.sql import Select

from app.core.tenant import TenantContext
from app.db.base import Base
from app.modules.marketplace.capability import require_marketplace_capability

ModelT = TypeVar("ModelT", bound=Base)


class MarketplaceBaseService:
    """无状态市场 Service 基类，使用可信上下文注入 tenant scope。"""

    def scoped(self, model: type[ModelT], *, tenant: TenantContext) -> Select:
        """所有 select 必须包一层，强制 tenant_id 过滤"""
        require_marketplace_capability(tenant)
        return select(model).where(model.tenant_id == tenant.tenant_id)
