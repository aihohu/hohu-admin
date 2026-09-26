# ruff: noqa: T201

"""
系统设置初始数据填充

按 setting_key 去重，只插入数据库中不存在的配置项，可安全重复执行。

Usage:
    cd hohu-admin
    python -m scripts.seed_settings
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID
from app.modules.system.models.setting import SystemSetting
from app.modules.system.settings_catalog import SETTINGS


def default_password_seed_value(env: str) -> str:
    return "" if env == "prod" else "Hohu123456"


def build_initial_settings(
    *, tenant_id: int, fresh: bool = False
) -> list[SystemSetting]:
    rows = []
    for definition in SETTINGS.values():
        value = str(definition.default)
        if definition.key == "auth:default_password":
            value = default_password_seed_value(settings.ENV)
        elif definition.key == "ai:enabled_tools" and fresh:
            value = '["file.parse"]'
        rows.append(
            SystemSetting(
                setting_id=next_id(),
                tenant_id=tenant_id,
                setting_key=definition.key,
                setting_value=value,
                status="1",
            )
        )
    return rows


async def seed_settings_in_session(
    db: AsyncSession, *, tenant_id: int, fresh: bool = False
):
    existing = set(
        (
            await db.scalars(
                select(SystemSetting.setting_key).where(
                    SystemSetting.tenant_id == tenant_id
                )
            )
        ).all()
    )
    for config in build_initial_settings(tenant_id=tenant_id, fresh=fresh):
        if config.setting_key not in existing:
            db.add(config)
    await db.flush()


async def seed_settings():
    engine = create_async_engine(settings.DATABASE_URL)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db, db.begin():
            await seed_settings_in_session(db, tenant_id=DEFAULT_TENANT_ID)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_settings())
