"""User compatibility and PostgreSQL query/lock contracts for the generic core."""

import asyncio
import json
from contextvars import ContextVar
from dataclasses import replace
from importlib import import_module

import pytest
from sqlalchemy import (
    Boolean,
    Integer,
    column,
    event,
    insert,
    literal,
    select,
    text,
    values,
)

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind, TenantLock
from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    Grant,
    ObjectSpec,
    RecordContext,
    compile_rules,
    project,
    query_counterexample,
    resolve_fields,
)
from app.db.authorization_plan_session import _known_read
from app.db.base import Base
from app.modules.system.field_policy.evaluator import (
    FieldGrant,
    project_user_values,
    resolve_record_fields,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.models.dict_type import DictType
from app.modules.system.models.file import File
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize(
    "role_ids",
    [
        frozenset(),
        frozenset({"1"}),
        frozenset({"2"}),
        frozenset({"1", "2"}),
        frozenset({"1", "3"}),
    ],
)
@pytest.mark.parametrize(
    "operation,purpose",
    [
        ("read", "management.read"),
        ("read", "own-account"),
        ("export", "export"),
        ("create", "management.create"),
        ("update", "management.update"),
        ("update", "own-account"),
        ("replace", "management.relations"),
        ("create", "import"),
        ("update", "import"),
        ("replace", "import"),
        ("exact", "management.query"),
        ("contains", "management.query"),
        ("eq", "management.query"),
        ("group", "management.query"),
        ("distinct", "management.query"),
        ("exact", "import"),
    ],
)
def test_legacy_per_record_decisions_have_explicit_generic_equivalent(
    role_ids, operation, purpose
):
    adapter = import_module("app.modules.system.field_policy.generic_compat")
    obj = adapter.user_compat_object()
    uses = tuple(
        FieldUse(FieldRef(obj.object_id, f.field_id), access)
        for f in obj.fields
        for access in f.accesses
    )
    catalog = Catalog(
        (obj,),
        (ActionSpec("compat.user", (obj.object_id,), uses, ("system:user:list",)),),
    )
    grants = []
    for field in obj.fields:
        for access in field.accesses:
            # Legacy query has no use; conversion declares its existing filter /
            # join paths explicitly, never a new sorting capability.
            if access.use == "join":
                continue
            for role, effect in (("1", "allow"), ("2", "allow"), ("3", "deny")):
                masked = (
                    role == "1"
                    and access.operation in {"read", "export"}
                    and field.mask_id
                )
                grants.append(
                    FieldGrant(
                        role,
                        field.field_id,
                        access.operation,
                        access.purpose,
                        effect,
                        "masked" if masked else "plain",
                        field.mask_id if masked else None,
                    )
                )
    old = resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=tuple(grants),
        applicable_role_ids=role_ids,
        operation=operation,
        purpose=purpose,
        entrypoint="ai-tool",
    )
    rules = compile_rules(catalog, adapter.convert_legacy_grants(0, tuple(grants)))
    access = adapter.legacy_accesses(operation, purpose)[0]
    ctx = RecordContext(
        0, obj.object_id, "101", role_ids, frozenset({"system:user:list"})
    )
    new = resolve_fields(catalog, rules, ctx, "compat.user", access)
    for field_id in USER_FIELDS:
        assert new[field_id].allowed == old.fields[field_id].allowed
        if operation in {"read", "export"}:
            assert new[field_id].level == old.fields[field_id].read
            assert new[field_id].mask_id == old.fields[field_id].mask_id
    if operation in {"read", "export"}:
        raw = {
            "user_id": 101,
            "user_phone": "13800138000",
            "user_email": "test@example.test",
            "nickname": None,
            "hashed_password": "never",
            "roles": [{"roleId": "201", "roleName": "Scoped upstream"}],
        }
        caps = dict.fromkeys(raw, "plain")
        expected = project_user_values(
            raw_values=raw, decisions=old, entrypoint_caps=caps
        )
        assert (
            project(catalog, rules, ctx, "compat.user", access, raw) == expected.values
        )


async def test_postgres_counterexample_checks_entire_scope_before_client_filter(
    plan_sessions,
):
    _, plain = plan_sessions
    access = Access("eq", "management.query", "filter")
    ref = FieldRef("demo.item", "status")
    catalog = Catalog(
        (
            ObjectSpec(
                ref.object_id,
                "tenant",
                "demo.scope",
                (FieldSpec("status", "string", (access,)),),
            ),
        ),
        (
            ActionSpec(
                "demo.list",
                (ref.object_id,),
                (FieldUse(ref, access),),
                ("demo:item:list",),
            ),
        ),
    )
    ctx = RecordContext(
        0, ref.object_id, "1", frozenset({"1", "2"}), frozenset({"demo:item:list"})
    )
    table = values(
        column("id", Integer),
        column("tenant_id", Integer),
        column("local_scope", Boolean),
        name="items",
    ).data([(1, 0, True), (2, 0, False), (3, 0, None), (4, 1, True)])
    scopes = {"1": table.c.local_scope, "2": table.c.id == 1}
    rules = compile_rules(
        catalog, (Grant(0, "1", ref, access), Grant(0, "2", ref, access, "deny"))
    )
    predicate = query_counterexample(
        catalog,
        rules,
        ctx,
        "demo.list",
        (FieldUse(ref, access),),
        table.c.tenant_id == 0,
        scopes,
    )
    assert _known_read(select(table.c.id).where(predicate))
    async with plain() as db:
        denied = (
            await db.scalars(select(table.c.id).where(predicate).order_by(table.c.id))
        ).all()
        assert denied == [1, 2, 3]
        # A hostile client filter could hide the unauthorized rows. It must not
        # be part of the base scope passed to the authorization proof.
        assert (
            await db.scalar(
                select(literal(True)).select_from(table).where(predicate).limit(1)
            )
            is True
        )
        assert (
            await db.scalars(select(table.c.id).where(predicate, table.c.id == 99))
        ).all() == []
    for record, applicable in (
        (1, frozenset({"1", "2"})),
        (2, frozenset()),
        (3, frozenset()),
    ):
        assert not resolve_fields(
            catalog,
            rules,
            replace(ctx, record_key=str(record), role_ids=applicable),
            "demo.list",
            access,
        )["status"].allowed


@pytest.mark.parametrize("finish", ["commit", "rollback", "cancel"])
async def test_opposite_file_dictionary_plans_wait_in_same_order_and_release(
    plan_sessions, finish, record_property
):
    factory, plain = plan_sessions
    engine = plain.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[DictType.__table__, File.__table__]
            )
        )
    async with plain() as db:
        await db.execute(
            insert(DictType).values(
                tenant_id=0, dict_type_id=501, dict_name="f111", dict_type="f111"
            )
        )
        await db.execute(
            insert(File).values(
                tenant_id=0,
                file_id=501,
                original_name="f111.txt",
                file_name="f111.txt",
                file_path="f111.txt",
                file_url="/f111.txt",
                file_size=1,
                file_ext="txt",
                owner_user_id=101,
            )
        )
        await db.commit()
    dictionary = ResourceKey(ResourceKind.DICT_TYPE, 0, 501)
    file = ResourceKey(ResourceKind.FILE, 0, 501)
    statements, pids = {}, {}
    sql_counts = {}
    current_owner = ContextVar("f111_test_owner", default=None)
    holder_ready, contender_started, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    def record(conn, _cursor, sql, _params, context, _many):
        label = current_owner.get()
        if label:
            pids[label] = conn.connection.driver_connection.get_server_pid()
            sql_counts[label] = sql_counts.get(label, 0) + 1
        if label and "FOR UPDATE" in sql:
            statements.setdefault(label, []).append(
                context.compiled.statement.get_final_froms()[0].name
            )
            if label == "contender":
                contender_started.set()

    event.listen(engine.sync_engine, "before_cursor_execute", record)

    async def run(label, keys):
        async with factory() as db:
            current_owner.set(label)
            async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
                await tx.lock(LockPlan(keys))
                tx.assert_held(dictionary, file)
                if label == "holder":
                    holder_ready.set()
                    await release.wait()
            if label == "holder" and finish == "rollback":
                await db.rollback()
            else:
                await db.commit()

    holder = asyncio.create_task(run("holder", (file, dictionary)))
    holder.add_done_callback(lambda _task: holder_ready.set())
    contender = None
    try:
        await asyncio.wait_for(holder_ready.wait(), 3)
        if holder.done():
            await holder
            pytest.fail("holder completed before the release signal")
        contender = asyncio.create_task(run("contender", (dictionary, file)))
        await asyncio.wait_for(contender_started.wait(), 3)
        # PostgreSQL itself must attest waiting on the holder; no elapsed sleep
        # or unfinished Python task is accepted as evidence of a row lock.
        async with plain() as observer, asyncio.timeout(1):
            while True:
                if contender.done():
                    await contender
                    pytest.fail("contender completed without waiting for the row lock")
                blockers = await observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["contender"]}
                )
                if pids["holder"] in blockers:
                    break
        assert statements["holder"] == ["sys_dict_type", "sys_file"]
        assert statements["contender"] == ["sys_dict_type"]
        if finish == "cancel":
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder
        else:
            release.set()
            await asyncio.wait_for(holder, 3)
        await asyncio.wait_for(contender, 3)
        assert statements["contender"] == ["sys_dict_type", "sys_file"]
        assert all(count <= 20 for count in sql_counts.values())
        record_property("lock_sql_counts", json.dumps(sql_counts, sort_keys=True))
    finally:
        release.set()
        for task in (holder, contender):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (holder, contender) if t is not None), return_exceptions=True
        )
        event.remove(engine.sync_engine, "before_cursor_execute", record)
