"""Independent transaction-proof regressions in disposable committed schemas."""

import asyncio
import copy
from contextlib import contextmanager

import pytest
from sqlalchemy import event, func, insert, literal, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import raiseload, with_expression
from sqlalchemy.pool import NullPool

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.session import engine
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.field_policy_test_support import isolated_field_policy_schema

USER = ResourceKey(ResourceKind.USER, 0, 101)
ROLE = ResourceKey(ResourceKind.ROLE, 0, 201)


@contextmanager
def rejected(code="FIELD_POLICY_LOCK_ORDER"):
    with pytest.raises(BusinessException) as caught:
        yield
    assert caught.value.error_code == code


@pytest.fixture
async def proof_sessions():
    base = create_async_engine(engine.url, poolclass=NullPool)
    try:
        async with isolated_field_policy_schema(base) as scoped:
            ordinary = async_sessionmaker(
                scoped, expire_on_commit=False, autoflush=False
            )
            managed = async_sessionmaker(
                scoped,
                class_=AuthorizationPlanSession,
                expire_on_commit=False,
                autoflush=False,
            )
            async with ordinary() as setup:
                for tenant_id in (0, 1):
                    await setup.execute(
                        insert(Tenant).values(
                            tenant_id=tenant_id,
                            tenant_code=f"proof-{tenant_id}",
                            tenant_name="Proof fixture",
                            status="1",
                            lifecycle_state="active",
                            bootstrap_version=1,
                            row_version=1,
                            bootstrap_key_hash="a" * 64 if tenant_id else None,
                            bootstrap_fingerprint="b" * 64 if tenant_id else None,
                        )
                    )
                    await setup.execute(
                        insert(User).values(
                            tenant_id=tenant_id,
                            user_id=101 + tenant_id,
                            user_name="existing",
                            hashed_password="fixture-only",
                            status="1",
                        )
                    )
                    await setup.execute(
                        insert(Role).values(
                            tenant_id=tenant_id,
                            role_id=201 + tenant_id,
                            role_name="existing",
                            role_code="EXISTING",
                            status="1",
                            data_scope="1",
                        )
                    )
                await setup.commit()
            yield managed, ordinary, scoped
    finally:
        await base.dispose()


def scope(db, *, exclusive=False):
    mode = LockMode.EXCLUSIVE if exclusive else LockMode.SHARED
    return authorization_transactions.scope(db, tenants=(TenantLock(0, mode=mode),))


async def unsafe_sql(db, kind):
    if kind == "orm_update":
        await db.execute(
            update(User).where(User.user_id == 101).values(nickname="changed")
        )
    elif kind == "core_update":
        await db.execute(
            User.__table__.update()
            .where(User.user_id == 101)
            .values(nickname="changed")
        )
    elif kind in {"for_update", "for_share"}:
        await db.scalar(
            select(User.user_id)
            .where(User.user_id == 101)
            .with_for_update(read=kind == "for_share")
        )
    elif kind == "connection_update":
        connection = await db.connection()
        await connection.execute(
            update(User).where(User.user_id == 101).values(nickname="changed")
        )
    elif kind == "driver_update":
        connection = await db.connection()
        await connection.exec_driver_sql(
            "UPDATE sys_user SET nickname = 'changed' WHERE user_id = 101"
        )
    elif kind == "driver_lock":
        connection = await db.connection()
        await connection.exec_driver_sql(
            "SELECT user_id FROM sys_user WHERE user_id = 101 FOR UPDATE"
        )
    elif kind == "unknown_function":
        await db.scalar(select(func.pg_try_advisory_xact_lock(941702)))
    elif kind == "write_cte":
        changed = (
            update(User)
            .where(User.user_id == 101)
            .values(nickname="changed")
            .returning(User.user_id)
            .cte("changed")
        )
        await db.scalar(select(changed.c.user_id))
    elif kind == "raw_select":
        await db.scalar(text("SELECT user_id FROM sys_user WHERE user_id = 101"))
    elif kind == "flush":
        db.add(User(tenant_id=0, user_id=301, user_name="flushed", hashed_password="x"))
        await db.flush()
    else:
        raise AssertionError(kind)


UNSAFE_SQL = (
    "orm_update",
    "core_update",
    "for_update",
    "for_share",
    "connection_update",
    "driver_update",
    "driver_lock",
    "unknown_function",
    "write_cte",
    "raw_select",
    "flush",
)


@pytest.mark.parametrize("kind", UNSAFE_SQL)
async def test_prior_sql_is_observed_even_when_orm_state_is_clean(proof_sessions, kind):
    managed, _, _ = proof_sessions
    async with managed() as db:
        await unsafe_sql(db, kind)
        assert not db.new and not db.dirty and not db.deleted
        with rejected():
            async with scope(db):
                pytest.fail("unsafe earlier SQL must prevent barrier acquisition")
        with rejected():
            await db.commit()
        await db.rollback()
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            tx.assert_held(USER)


async def test_plain_typed_read_and_zero_sql_assertion(proof_sessions):
    managed, _, scoped = proof_sessions
    async with managed() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 101)) == 101
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            statements = []

            def capture(*args):
                statements.append(args[2])

            event.listen(scoped.sync_engine, "before_cursor_execute", capture)
            try:
                tx.assert_held(USER)
                async with tx.savepoint():
                    statements.clear()
                    tx.assert_held(USER)
                    assert statements == []
                statements.clear()
                tx.assert_held(USER)
                assert statements == []
            finally:
                event.remove(scoped.sync_engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("kind", UNSAFE_SQL)
async def test_active_scope_rejects_direct_sql_and_owner_cannot_commit(
    proof_sessions, kind
):
    managed, ordinary, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db, exclusive=True) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                await unsafe_sql(db, kind)
        with rejected():
            await db.commit()
        await db.rollback()
    async with ordinary() as inspect:
        assert (
            await inspect.scalar(select(User.nickname).where(User.user_id == 101))
            is None
        )
        assert (
            await inspect.scalar(select(User.user_id).where(User.user_id == 301))
            is None
        )


@pytest.mark.parametrize("mutation", ["copy", "blank", "session_info"])
async def test_proof_identity_cannot_be_forged(proof_sessions, mutation):
    managed, _, _ = proof_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            if mutation == "session_info":
                db.info["authorization_transaction"] = {"held": {ROLE, USER}}
                db.info["user_field_policy_barriers"] = {"tenants": {0: "exclusive"}}
                with rejected():
                    tx.assert_held(ROLE)
            else:
                try:
                    forged = (
                        copy.copy(tx)
                        if mutation == "copy"
                        else object.__new__(type(tx))
                    )
                except TypeError:
                    # A deliberately noncopyable/nonconstructible handle is valid.
                    return
                with rejected():
                    forged.assert_held(USER)
            tx.assert_held(USER)
        await db.rollback()


@pytest.mark.parametrize("invalid", [{}, [], None, 101, "USER:0:101"])
async def test_assert_held_invalid_resource_is_nonfatal_domain_error(
    proof_sessions, invalid
):
    managed, _, _ = proof_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            with rejected():
                tx.assert_held(invalid)
            tx.assert_held(USER)
        await db.commit()


@pytest.mark.parametrize("ending", ["commit", "rollback", "close"])
async def test_finished_outer_and_session_reuse_never_revive_handle(
    proof_sessions, ending
):
    managed, _, _ = proof_sessions
    async with managed() as db:
        async with scope(db) as old:
            await old.lock(LockPlan(existing=(USER,)))
        with rejected():
            old.assert_held(USER)
        await getattr(db, ending)()
        async with scope(db) as current:
            await current.lock(LockPlan(existing=(USER,)))
            current.assert_held(USER)
            with rejected():
                old.assert_held(USER)


async def test_unmanaged_session_is_rejected_even_if_no_prior_sql(proof_sessions):
    _, ordinary, _ = proof_sessions
    async with ordinary() as db:
        with rejected():
            async with scope(db):
                pytest.fail("plain AsyncSession cannot mint observed proof")


async def test_external_connection_binding_is_rejected(proof_sessions):
    _, _, scoped = proof_sessions
    async with scoped.connect() as connection:
        async with connection.begin():
            with rejected():
                async with AuthorizationPlanSession(bind=connection) as db:
                    async with scope(db):
                        pytest.fail("shared external transaction cannot mint proof")


async def test_lock_plan_cannot_be_extended_after_first_lock(proof_sessions):
    managed, _, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                await tx.lock(LockPlan(existing=(ROLE,)))
        await db.rollback()


@pytest.mark.parametrize("method", ["begin_nested", "raw_savepoint"])
async def test_uncontrolled_savepoint_is_rejected(proof_sessions, method):
    managed, _, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db, exclusive=True) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                if method == "begin_nested":
                    await db.begin_nested()
                else:
                    await db.execute(text("SAVEPOINT bypass_plan"))
        await db.rollback()


async def test_pending_orm_state_cannot_be_flushed_by_savepoint(proof_sessions):
    managed, ordinary, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db, exclusive=True) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                db.add(
                    User(
                        tenant_id=0,
                        user_id=301,
                        user_name="pending",
                        hashed_password="x",
                    )
                )
                async with tx.savepoint():
                    pytest.fail("begin_nested must not flush pending objects")
        await db.rollback()
    async with ordinary() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 301)) is None


def new_user(identity):
    return ResourceKey(ResourceKind.USER, 0, identity)


def user_values(name):
    return {"user_name": name, "hashed_password": "fixture-only", "status": "1"}


async def test_new_row_held_only_after_successful_reserved_insert(proof_sessions):
    managed, ordinary, _ = proof_sessions
    key = new_user(301)
    async with managed() as db:
        async with scope(db, exclusive=True) as tx:
            await tx.lock(LockPlan(existing=(USER,), new=(NewResource(key),)))
            with rejected():
                tx.assert_held(key)
            await tx.insert_reserved(key, user_values("created"))
            tx.assert_held(key)
        await db.commit()
    async with ordinary() as db:
        assert (
            await db.scalar(select(User.user_name).where(User.user_id == 301))
            == "created"
        )


async def test_row_release_then_chunk_rollback_revokes_only_new_row_proofs(
    proof_sessions,
):
    managed, ordinary, _ = proof_sessions
    key = new_user(301)
    async with managed() as db:
        async with scope(db, exclusive=True) as tx:
            await tx.lock(LockPlan(existing=(USER,), new=(NewResource(key),)))
            with pytest.raises(ValueError, match="rollback chunk"):
                async with tx.savepoint():
                    async with tx.savepoint():
                        await tx.insert_reserved(key, user_values("rolled-back"))
                        tx.assert_held(key)
                    tx.assert_held(key)
                    raise ValueError("rollback chunk")
            tx.assert_held(USER)
            with rejected():
                tx.assert_held(key)
        await db.commit()
    async with ordinary() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 301)) is None


async def test_unique_violation_in_savepoint_does_not_poison_outer(proof_sessions):
    managed, ordinary, _ = proof_sessions
    bad, good = new_user(301), new_user(302)
    async with managed() as db:
        async with scope(db, exclusive=True) as tx:
            await tx.lock(
                LockPlan(existing=(USER,), new=(NewResource(bad), NewResource(good)))
            )
            with pytest.raises(IntegrityError):
                async with tx.savepoint():
                    await tx.insert_reserved(bad, user_values("existing"))
            tx.assert_held(USER)
            with rejected():
                tx.assert_held(bad)
            async with tx.savepoint():
                await tx.insert_reserved(good, user_values("unique"))
            tx.assert_held(good)
        await db.commit()
    async with ordinary() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 301)) is None
        assert (
            await db.scalar(select(User.user_name).where(User.user_id == 302))
            == "unique"
        )


@pytest.mark.parametrize("override", [{"tenant_id": 1}, {"user_id": 999}])
async def test_reserved_insert_cannot_override_identity(proof_sessions, override):
    managed, _, _ = proof_sessions
    key = new_user(301)
    async with managed() as db:
        with rejected():
            async with scope(db, exclusive=True) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
                await tx.insert_reserved(key, {**user_values("forged"), **override})
        await db.rollback()


async def test_scope_exit_disallows_more_sql_before_owner_commit(proof_sessions):
    managed, _, _ = proof_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
        with rejected():
            await db.scalar(select(User.user_id).where(User.user_id == 101))
        await db.rollback()


async def test_copying_session_info_does_not_transfer_another_sessions_proof(
    proof_sessions,
):
    managed, _, _ = proof_sessions
    async with managed() as first, managed() as other:
        async with scope(first) as first_tx:
            await first_tx.lock(LockPlan(existing=(USER,)))
            other.info.update(first.info)
            async with scope(other) as other_tx:
                await other_tx.lock(LockPlan())
                with rejected():
                    other_tx.assert_held(USER)
                first_tx.assert_held(USER)


async def test_failed_owner_commit_never_revives_exited_scope(proof_sessions):
    managed, ordinary, _ = proof_sessions
    key = new_user(301)
    async with managed() as db:
        async with scope(db, exclusive=True) as old:
            await old.lock(LockPlan(new=(NewResource(key),)))
            await old.insert_reserved(key, user_values("never-committed"))

        def fail_before_commit(_session):
            raise RuntimeError("injected owner commit failure")

        event.listen(db.sync_session, "before_commit", fail_before_commit)
        try:
            with pytest.raises(RuntimeError, match="injected owner commit failure"):
                await db.commit()
        finally:
            event.remove(db.sync_session, "before_commit", fail_before_commit)
        with rejected():
            old.assert_held(key)
        await db.rollback()
        async with scope(db) as current:
            await current.lock(LockPlan(existing=(USER,)))
            current.assert_held(USER)
            with rejected():
                old.assert_held(key)
    async with ordinary() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 301)) is None


@pytest.mark.parametrize("retry", ["session", "connection"])
async def test_caught_owner_commit_failure_requires_rollback(proof_sessions, retry):
    managed, ordinary, _ = proof_sessions
    key = new_user(301)
    async with managed() as db:
        connection = await db.connection()
        async with scope(db, exclusive=True) as tx:
            await tx.lock(LockPlan(new=(NewResource(key),)))
            await tx.insert_reserved(key, user_values("must-rollback"))

        def fail_before_commit(_session):
            raise RuntimeError("commit interrupted before driver")

        event.listen(db.sync_session, "before_commit", fail_before_commit)
        try:
            with pytest.raises(RuntimeError, match="commit interrupted"):
                await db.commit()
        finally:
            event.remove(db.sync_session, "before_commit", fail_before_commit)
        with rejected():
            await (db.commit() if retry == "session" else connection.commit())
        await db.rollback()
    async with ordinary() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 301)) is None


@pytest.mark.parametrize("expression", ["subquery", "function", "text", "duck"])
async def test_reserved_insert_accepts_data_not_executable_sql(
    proof_sessions, expression
):
    managed, ordinary, _ = proof_sessions

    class SqlLike:
        def __clause_element__(self):
            return func.lower("expression")

    expressions = {
        "subquery": select(User.user_name).where(User.tenant_id == 1).scalar_subquery(),
        "function": func.lower("expression"),
        "text": text("current_database()"),
        "duck": SqlLike(),
    }
    key = new_user(301)
    async with managed() as db:
        with rejected():
            async with scope(db, exclusive=True) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
                await tx.insert_reserved(
                    key,
                    {**user_values("expression"), "nickname": expressions[expression]},
                )
        with rejected():
            await db.commit()
        await db.rollback()
    async with ordinary() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 301)) is None


async def test_reserved_json_values_recursively_reject_sql_expressions(proof_sessions):
    managed, _, _ = proof_sessions
    key = ResourceKey(ResourceKind.ROLE, 0, 301)
    async with managed() as db:
        with rejected():
            async with scope(db, exclusive=True) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
                await tx.insert_reserved(
                    key,
                    {
                        "role_name": "new",
                        "role_code": "NEW",
                        "status": "1",
                        "i18n_keys": {"en": [func.current_database()]},
                    },
                )
        await db.rollback()


@pytest.mark.parametrize(
    "escape", ["raw", "run_sync", "sync_connection", "commit", "rollback", "invalidate"]
)
async def test_public_connection_facade_cannot_escape_protocol(proof_sessions, escape):
    managed, _, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                connection = await db.connection()
                if escape == "raw":
                    await connection.get_raw_connection()
                elif escape == "run_sync":
                    await connection.run_sync(
                        lambda sync: sync.exec_driver_sql("SELECT 1")
                    )
                elif escape == "sync_connection":
                    _ = connection.sync_connection
                elif escape == "commit":
                    await connection.commit()
                elif escape == "invalidate":
                    await connection.invalidate()
                else:
                    await connection.rollback()
        with rejected():
            await db.commit()
        await db.rollback()


async def test_connection_obtained_before_scope_cannot_expose_raw_driver(
    proof_sessions,
):
    managed, _, _ = proof_sessions
    async with managed() as db:
        early_connection = await db.connection()
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                await early_connection.get_raw_connection()
        with rejected():
            await db.commit()
        await db.rollback()


@pytest.mark.parametrize("surface", ["session", "connection"])
@pytest.mark.parametrize("cleanup", ["rollback", "close"])
async def test_caught_typed_read_database_error_poison_is_not_cleared_by_savepoint(
    proof_sessions, surface, cleanup
):
    managed, _, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db, exclusive=True) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                async with tx.savepoint():
                    reader = db if surface == "session" else await db.connection()
                    with pytest.raises(DBAPIError):
                        await reader.scalar(select(literal(1) / literal(0)))
                    with rejected():
                        tx.assert_held(USER)
        with rejected():
            await db.commit()
        await getattr(db, cleanup)()
        with rejected():
            tx.assert_held(USER)
        async with scope(db) as recovered:
            await recovered.lock(LockPlan(existing=(USER,)))
            recovered.assert_held(USER)


async def test_cancelled_barrier_wait_retains_cancel_and_requires_rollback(
    proof_sessions,
):
    managed, ordinary, scoped = proof_sessions
    attempted = asyncio.Event()

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if "sys_user_field_policy_state" in statement and "FOR SHARE" in statement:
            attempted.set()

    async with ordinary() as holder, managed() as waiting:
        await holder.scalar(
            select(UserFieldPolicyState.tenant_id)
            .where(UserFieldPolicyState.tenant_id == 0)
            .with_for_update()
        )

        async def enter():
            async with scope(waiting):
                pytest.fail("B is still held exclusively")

        event.listen(scoped.sync_engine, "before_cursor_execute", capture)
        task = asyncio.create_task(enter())
        try:
            await asyncio.wait_for(attempted.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with rejected():
                await waiting.commit()
            await waiting.rollback()
            await holder.rollback()
            async with scope(waiting) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                tx.assert_held(USER)
        finally:
            event.remove(scoped.sync_engine, "before_cursor_execute", capture)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_orm_load_with_raiseload_is_safe_before_scope(proof_sessions):
    managed, _, _ = proof_sessions
    async with managed() as db:
        user = await db.scalar(
            select(User).options(raiseload("*")).where(User.user_id == 101)
        )
        assert user.user_id == 101
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            tx.assert_held(USER)


async def test_public_transaction_proxy_commit_failure_cannot_be_retried(
    proof_sessions,
):
    managed, ordinary, _ = proof_sessions
    key = new_user(301)
    async with managed() as db:
        async with scope(db, exclusive=True) as tx:
            await tx.lock(LockPlan(new=(NewResource(key),)))
            await tx.insert_reserved(key, user_values("proxy-must-rollback"))
        proxy = db.get_transaction()

        def fail_before_commit(_session):
            raise RuntimeError("public transaction commit interrupted")

        event.listen(db.sync_session, "before_commit", fail_before_commit)
        try:
            # A restricted public facade may reject before the injected failure.
            with pytest.raises((RuntimeError, BusinessException)) as caught:
                await proxy.commit()
            if isinstance(caught.value, BusinessException):
                assert caught.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        finally:
            event.remove(db.sync_session, "before_commit", fail_before_commit)
        with rejected():
            await db.commit()
        await db.rollback()
    async with ordinary() as db:
        assert await db.scalar(select(User.user_id).where(User.user_id == 301)) is None


async def test_merge_read_failure_cannot_be_caught_to_reuse_proof(proof_sessions):
    managed, _, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                async with tx.savepoint():
                    with pytest.raises((DBAPIError, BusinessException)) as caught:
                        await db.merge(
                            User(user_id=101),
                            options=[
                                raiseload("*"),
                                with_expression(User.nickname, literal(1) / literal(0)),
                            ],
                        )
                    if isinstance(caught.value, BusinessException):
                        assert caught.value.error_code == "FIELD_POLICY_LOCK_ORDER"
                    with rejected():
                        tx.assert_held(USER)
        with rejected():
            await db.commit()
        await db.rollback()


@pytest.mark.parametrize("method", ["merge", "delete", "reset", "invalidate"])
async def test_unsupported_public_session_methods_are_fatal(proof_sessions, method):
    managed, _, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                user = await db.scalar(
                    select(User).options(raiseload("*")).where(User.user_id == 101)
                )
                with rejected():
                    if method in {"merge", "delete"}:
                        await getattr(db, method)(user)
                    else:
                        await getattr(db, method)()
                with rejected():
                    tx.assert_held(USER)
        with rejected():
            await db.commit()
        await db.rollback()


@pytest.mark.parametrize("surface", ["begin", "begin_nested", "outer", "nested"])
@pytest.mark.parametrize("action", ["commit", "rollback"])
async def test_public_transaction_control_is_rejected(proof_sessions, surface, action):
    managed, _, _ = proof_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                async with tx.savepoint():
                    with rejected():
                        if surface in {"begin", "begin_nested"}:
                            await getattr(db, surface)()
                        else:
                            proxy = (
                                db.get_transaction()
                                if surface == "outer"
                                else db.get_nested_transaction()
                            )
                            assert proxy.is_active
                            await getattr(proxy, action)()
                    with rejected():
                        tx.assert_held(USER)
        with rejected():
            await db.commit()
        await db.rollback()


@pytest.mark.parametrize("stage", ["savepoint", "release_savepoint"])
async def test_savepoint_control_failure_cannot_be_caught_to_reuse_proof(
    proof_sessions, stage
):
    managed, _, scoped = proof_sessions

    def fail_control(*_args):
        raise RuntimeError("injected savepoint control failure")

    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                event.listen(scoped.sync_engine, stage, fail_control)
                try:
                    with pytest.raises(RuntimeError, match="savepoint control failure"):
                        async with tx.savepoint():
                            tx.assert_held(USER)
                finally:
                    event.remove(scoped.sync_engine, stage, fail_control)
                with rejected():
                    tx.assert_held(USER)
        with rejected():
            await db.commit()
        await db.rollback()
        async with scope(db) as recovered:
            await recovered.lock(LockPlan(existing=(USER,)))
            recovered.assert_held(USER)
