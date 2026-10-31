"""Committed concurrency fixtures in a disposable, individually named schema."""

import re
import runpy
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.base import Base
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic/versions/e0202a000001_user_field_policy_storage.py"
)


def _install(connection) -> None:
    Base.metadata.create_all(
        connection, tables=[Tenant.__table__, Role.__table__, User.__table__]
    )
    migration = runpy.run_path(str(_MIGRATION))
    with Operations.context(MigrationContext.configure(connection)):
        migration["upgrade"]()


@asynccontextmanager
async def isolated_field_policy_schema(base_engine: AsyncEngine):
    """Allow real commits without deleting append-only facts from shared tables.

    The caller owns the dedicated test database. Every new connection receives
    this schema as its server search_path; its pool never touches the base pool.
    """
    if base_engine.dialect.name != "postgresql":
        raise ValueError("Field policy storage tests require PostgreSQL")
    schema = "ufp_v0202a_" + uuid4().hex
    scoped_engine = None
    try:
        async with base_engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            await connection.execute(
                text("SELECT set_config('search_path', :schema, true)"),
                {"schema": schema},
            )
            await connection.run_sync(_install)
        scoped_engine = create_async_engine(
            base_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        yield scoped_engine
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        if re.fullmatch(r"ufp_v0202a_[0-9a-f]{32}", schema) is None:
            raise ValueError("Refusing to drop an unexpected test schema")
        async with base_engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
