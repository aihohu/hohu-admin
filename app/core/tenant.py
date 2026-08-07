"""可信租户解析入口。

当前 HoHu 认证域仍是单租户，唯一合法租户为 ``0``。集中保留这个入口，
避免 Chat body、tool args、multipart form 等客户端字段被误当成授权依据；未来
认证域正式引入租户成员关系时，只在这里改为读取已认证 principal 的可信声明。
"""

from typing import Any

DEFAULT_TENANT_ID = 0


def resolve_tenant_id(_authenticated_user: Any) -> int:
    """从已认证 principal 解析租户；当前单租户恒为服务端常量 0。"""
    return DEFAULT_TENANT_ID
