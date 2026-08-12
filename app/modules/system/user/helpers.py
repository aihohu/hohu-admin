"""User import/export helpers（Task 3，spec §2.5 / §3.6）。

依赖反转层：业务层（import_service / export_service）通过 helpers 调用
model 查询，避免重复样板代码 + 集中安全策略（默认密码缺失抛异常而不是
返回硬编码值）。

注意：本模块直接查 sys_config 不走 config_service.get_value 的 redis 缓存。
理由：import 是低频管理操作（admin UI 触发），引入 cache 会让单测间数据
污染（cacheable ttl=300s），且 admin UI 改了 default_password 后下一次导入
立刻读到新值，更符合「敏感配置即时生效」语义。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.system.models.config import Config

#: sys_config 中默认密码的 key（spec §2.5 / §3.6 line 2093）。
DEFAULT_PASSWORD_CONFIG_KEY = "auth:default_password"
INSECURE_DEFAULT_PASSWORD_SENTINELS = frozenset({"Hohu123456"})
"""公开开发种子；生产环境必须显式改掉，不能用于创建或重置账号。"""


async def get_default_password(db: AsyncSession) -> str:
    """读取 sys_config.auth:default_password（spec §2.5）。

    所有新用户用本值哈希入库；管理员线下告知用户初始密码。
    缺失 / 禁用 → 抛 ``AI_IMPORT_DEFAULT_PASSWORD_NOT_SET``：
    - 防 admin 误以为已配置但实际未配置导致导入后无法登录
    - 防代码硬编码默认密码安全风险（spec §2.5 反例 3）

    Returns:
        配置值（明文，调用方负责 ``get_password_hash`` 哈希入库）
    """
    result = await db.execute(
        select(Config.config_value).where(
            Config.config_key == DEFAULT_PASSWORD_CONFIG_KEY,
            Config.status == "1",  # noqa: E712
        )
    )
    value = result.scalar_one_or_none()
    if value is None or not value.strip():
        raise BusinessRuleException(
            "默认密码未配置（sys_config.auth:default_password），无法导入新用户",
            error_code="AI_IMPORT_DEFAULT_PASSWORD_NOT_SET",
        )
    if settings.ENV == "prod" and value in INSECURE_DEFAULT_PASSWORD_SENTINELS:
        raise BusinessRuleException(
            "生产环境禁止使用公开的初始化默认密码，请先更新 auth:default_password",
            error_code="AI_IMPORT_DEFAULT_PASSWORD_INVALID",
        )
    return value
