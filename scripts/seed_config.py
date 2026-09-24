# ruff: noqa: T201

"""
系统配置初始数据填充

按 config_key 去重，只插入数据库中不存在的配置项，可安全重复执行。

Usage:
    cd hohu-admin
    python scripts/seed_config.py
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID
from app.modules.system.models.config import Config

CONFIG_SEED_DATA = [
    {
        "config_name": "AI 额外启用工具",
        "config_key": "ai:enabled_tools",
        "config_value": "[]",
        "config_type": "text",
        "config_group": "ai",
        "status": "1",
        "is_public": False,
        "remark": "升级仅在缺失时创建；不自动启用 file.parse",
    },
    # ============ 基础配置（公开） ============
    {
        "config_name": "网站名称",
        "config_key": "site_name",
        "config_value": "后台管理系统",
        "config_type": "text",
        "config_group": "basic",
        "status": "1",
        "is_public": True,
        "remark": "网站标题，显示在浏览器标签页和登录页",
    },
    {
        "config_name": "网站 Logo",
        "config_key": "site_logo",
        "config_value": "",
        "config_type": "file",
        "config_group": "basic",
        "status": "1",
        "is_public": True,
        "remark": "网站 Logo 图片地址",
    },
    {
        "config_name": "网站描述",
        "config_key": "site_description",
        "config_value": "通用后台管理系统",
        "config_type": "text",
        "config_group": "basic",
        "status": "1",
        "is_public": True,
        "remark": "网站描述信息",
    },
    {
        "config_name": "ICP 备案号",
        "config_key": "site_icp",
        "config_value": "",
        "config_type": "text",
        "config_group": "basic",
        "status": "1",
        "is_public": True,
        "remark": "ICP 备案号，显示在页面底部",
    },
    {
        "config_name": "版权信息",
        "config_key": "site_copyright",
        "config_value": "Copyright © 2026",
        "config_type": "text",
        "config_group": "basic",
        "status": "1",
        "is_public": True,
        "remark": "页脚版权信息",
    },
    # ============ 协议配置（公开） ============
    {
        "config_name": "用户协议",
        "config_key": "user_agreement",
        "config_value": "",
        "config_type": "richtext",
        "config_group": "agreement",
        "status": "1",
        "is_public": True,
        "remark": "注册时展示的用户协议内容",
    },
    {
        "config_name": "隐私政策",
        "config_key": "privacy_policy",
        "config_value": "",
        "config_type": "richtext",
        "config_group": "agreement",
        "status": "1",
        "is_public": True,
        "remark": "注册时展示的隐私政策内容",
    },
    # ============ 功能配置（非公开） ============
    {
        "config_name": "默认头像",
        "config_key": "default_avatar",
        "config_value": "",
        "config_type": "file",
        "config_group": "feature",
        "status": "1",
        "is_public": False,
        "remark": "用户默认头像地址",
    },
    {
        "config_name": "开放注册",
        "config_key": "register_enabled",
        "config_value": "true",
        "config_type": "text",
        "config_group": "feature",
        "status": "1",
        "is_public": False,
        "remark": "是否开放用户自主注册，值为 true 或 false",
    },
    {
        "config_name": "强制用户主部门",
        "config_key": "user_require_primary_dept",
        "config_value": "false",
        "config_type": "text",
        "config_group": "feature",
        "status": "1",
        "is_public": False,
        "remark": "是否强制用户必须分配主部门，值为 true 或 false。开启后创建/编辑用户时必须指定主部门，且部门用户管理中移除成员不可导致用户无部门",
    },
]


def default_password_seed_value(env: str) -> str:
    return "" if env == "prod" else "Hohu123456"


def build_initial_configs(*, tenant_id: int, fresh: bool = False) -> list[Config]:
    definitions = [dict(item) for item in CONFIG_SEED_DATA]
    definitions.append(
        {
            "config_name": "导入用户默认密码",
            "config_key": "auth:default_password",
            "config_value": default_password_seed_value(settings.ENV),
            "config_type": "text",
            "config_group": "auth",
            "status": "1",
            "is_public": False,
            "remark": "安全提示：上线前修改导入用户默认密码；生产环境必须显式配置。",
        }
    )
    for item in definitions:
        if item["config_key"] == "ai:enabled_tools" and fresh:
            item["config_value"] = '["file.parse"]'
    return [
        Config(config_id=next_id(), tenant_id=tenant_id, **item) for item in definitions
    ]


async def seed_config_in_session(
    db: AsyncSession, *, tenant_id: int, fresh: bool = False
):
    existing = set(
        (
            await db.scalars(
                select(Config.config_key).where(Config.tenant_id == tenant_id)
            )
        ).all()
    )
    for config in build_initial_configs(tenant_id=tenant_id, fresh=fresh):
        if config.config_key not in existing:
            db.add(config)
    await db.flush()


async def seed_config():
    engine = create_async_engine(settings.DATABASE_URL)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db, db.begin():
            await seed_config_in_session(db, tenant_id=DEFAULT_TENANT_ID)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_config())
