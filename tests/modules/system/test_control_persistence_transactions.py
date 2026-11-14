"""Publication and privacy facts cannot bypass the complete resource plan."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db.base import Base
from app.modules.system.models.config import Config
from app.modules.system.models.config_publication import ConfigPublication
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def publication_sessions(plan_sessions):
    factory, plain = plan_sessions
    async with plain.kw["bind"].begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Config.__table__, ConfigPublication.__table__]
            )
        )
    async with plain() as db:
        await db.execute(
            insert(Config).values(
                tenant_id=0,
                config_id=301,
                config_name="Private",
                config_key="business.private",
                config_group="business",
                config_value="old",
                value_sensitivity="secret",
            )
        )
        await db.commit()
    return factory, plain


def provenance():
    return {
        "schema_revision": "v1",
        "content_digest": "a" * 64,
        "publication_version": 1,
        "published_by_user_id": 101,
        "published_at": datetime.now(UTC),
    }


@pytest.mark.parametrize("missing", ["config", "issuer", None])
async def test_publication_requires_config_and_current_issuer_in_same_plan(
    publication_sessions, missing
):
    factory, plain = publication_sessions
    pub = ResourceKey(ResourceKind.CONFIG_PUBLICATION, 0, 301)
    parents = {
        "config": ResourceKey(ResourceKind.CONFIG, 0, 301),
        "issuer": ResourceKey(ResourceKind.USER, 0, 101),
    }
    async with factory() as db:
        try:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(
                    LockPlan(
                        existing=tuple(
                            key for name, key in parents.items() if name != missing
                        ),
                        new=(NewResource(pub),),
                    )
                )
                async with tx.aggregate(0):
                    await tx.insert_reserved(pub, provenance())
            if missing:
                pytest.fail("Publication inserted without parent lock")
            await db.commit()
        except BusinessException as error:
            assert missing and error.error_code == "FIELD_POLICY_LOCK_ORDER"
            await db.rollback()
    async with plain() as db:
        assert len((await db.scalars(select(ConfigPublication))).all()) == (
            0 if missing else 1
        )
        assert await db.scalar(
            select(UserFieldPolicyState.epoch).where(
                UserFieldPolicyState.tenant_id == 0
            )
        ) == (1 if missing else 2)


async def test_secret_classification_cannot_be_lowered_by_controlled_writer(
    publication_sessions,
):
    factory, plain = publication_sessions
    key = ResourceKey(ResourceKind.CONFIG, 0, 301)
    async with factory() as db:
        with pytest.raises(BusinessException) as denied:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(LockPlan(existing=(key,)))
                async with tx.aggregate(0):
                    await tx.update_held(
                        key,
                        {"value_sensitivity": "private"},
                        expected={"value_sensitivity": "secret"},
                    )
        assert denied.value.error_code == "CONFIG_CLASSIFICATION_STICKY"
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(Config.value_sensitivity)) == "secret"


async def test_publication_cas_and_explicit_child_delete(publication_sessions):
    factory, plain = publication_sessions
    config = ResourceKey(ResourceKind.CONFIG, 0, 301)
    pub = ResourceKey(ResourceKind.CONFIG_PUBLICATION, 0, 301)
    issuer = ResourceKey(ResourceKind.USER, 0, 101)
    async with plain() as db:
        await db.execute(
            insert(ConfigPublication).values(tenant_id=0, config_id=301, **provenance())
        )
        await db.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as stale:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(LockPlan(existing=(config, pub, issuer)))
                async with tx.aggregate(0):
                    await tx.update_held(
                        pub,
                        {"publication_version": 2},
                        expected={"publication_version": 0},
                    )
        assert stale.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
        await db.rollback()
    async with factory() as db:
        with pytest.raises(BusinessException) as incomplete:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(LockPlan(existing=(config,)))
                async with tx.aggregate(0):
                    await tx.delete_held(config)
        assert incomplete.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        await db.rollback()
    async with factory() as db:
        async with transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            await tx.lock(LockPlan(existing=(config, pub, issuer)))
            async with tx.aggregate(0):
                await tx.update_held(
                    pub, {"publication_version": 2}, expected={"publication_version": 1}
                )
                await tx.delete_held(pub)
                await tx.delete_held(config)
        await db.commit()
    async with plain() as db:
        assert list(await db.scalars(select(Config.config_id))) == []
        assert list(await db.scalars(select(ConfigPublication.config_id))) == []
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )
