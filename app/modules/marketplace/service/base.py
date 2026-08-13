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

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class MarketplaceBaseService:
    """市场 Service 基类，自动注入 tenant_id 过滤"""

    def __init__(self, tenant_id: int = 0):
        self.tenant_id = tenant_id

    def scoped(self, model: type[ModelT]) -> Select:
        """所有 select 必须包一层，强制 tenant_id 过滤"""
        return select(model).where(model.tenant_id == self.tenant_id)
