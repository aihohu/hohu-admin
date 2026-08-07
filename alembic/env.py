import asyncio
import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.modules.marketplace.models  # noqa: F401
from alembic import context

# Import your model
from app.db.base import Base
from app.modules.ai.models.conversation import AiConversation  # noqa: F401
from app.modules.ai.models.message import AiMessage  # noqa: F401
from app.modules.ai.models.prepared_action import AiPreparedAction  # noqa: F401
from app.modules.ai.models.provider import AiProvider  # noqa: F401
from app.modules.job.models.job import SysJob, SysJobLog  # noqa: F401
from app.modules.system.models.dept import Dept  # noqa: F401
from app.modules.system.models.login_log import SysLoginLog  # noqa: F401
from app.modules.system.models.menu import Menu  # noqa: F401
from app.modules.system.models.operation_log import SysOperationLog  # noqa: F401
from app.modules.system.models.role import Role  # noqa: F401
from app.modules.system.models.user import User  # noqa: F401
from app.modules.system.user.models import (  # noqa: F401
    UserExportTask,
    UserImportBatch,
    UserImportBatchLog,
)

load_dotenv()
config = context.config

# If sqlalchemy.url is not set in alembic.ini, it will be obtained from environment variables.
if config.get_main_option("sqlalchemy.url") is None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL not set in environment")
    config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """实际执行迁移的同步辅助函数"""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：异步处理逻辑"""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
