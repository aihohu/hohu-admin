"""Disposable migrated schemas for tests that need independent real commits."""

import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal

_MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic"


def _install_migrations(connection) -> None:
    revisions = ScriptDirectory(str(_MIGRATIONS)).walk_revisions()
    with Operations.context(MigrationContext.configure(connection)):
        for revision in reversed(list(revisions)):
            revision.module.upgrade()


@asynccontextmanager
async def isolated_migrated_schema(database_url):
    """Build the actual schema, then drop only this fixture's owned namespace."""
    schema = "testcommit_" + uuid4().hex
    owner = create_async_engine(database_url, poolclass=NullPool)
    scoped = None
    if owner.dialect.name != "postgresql":
        await owner.dispose()
        raise ValueError("Committed database tests require PostgreSQL")
    try:
        async with owner.begin() as connection:
            await connection.execute(CreateSchema(schema))
            await connection.execute(
                text("SELECT set_config('search_path', :schema, true)"),
                {"schema": schema},
            )
            await connection.run_sync(_install_migrations)
        scoped = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        yield scoped
    finally:
        if scoped is not None:
            await scoped.dispose()
        try:
            if re.fullmatch(r"testcommit_[0-9a-f]{32}", schema) is None:
                raise ValueError("Refusing to drop an unexpected test schema")
            async with owner.begin() as connection:
                await connection.execute(
                    DropSchema(schema, cascade=True, if_exists=True)
                )
        finally:
            await owner.dispose()


@pytest.fixture
async def committed_test_database():
    """Rebind the shared factory, including aliases imported by app modules.

    Each test keeps real transaction boundaries on separate connections. Restoring
    the same factory avoids leaking the scoped engine into subsequent tests.
    """
    original_bind = AsyncSessionLocal.kw["bind"]
    async with isolated_migrated_schema(original_bind.url) as scoped:
        AsyncSessionLocal.configure(bind=scoped)
        try:
            yield scoped
        finally:
            AsyncSessionLocal.configure(bind=original_bind)
