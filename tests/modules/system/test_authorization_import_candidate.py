"""Import candidates retain row semantics inside one planned authorization epoch."""

import asyncio

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import BusinessException
from app.core.file_storage import ArtifactExecution, LocalFileStorage
from app.core.tenant import TenantContext
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.authorization_system_owner import system_candidate_owner
from app.db.base import role_menus, user_depts, user_roles
from app.db.session import engine
from app.modules.system.constants import EmployeeNoSyncMode, ImportBatchStatus
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.models.user_transfer import UserImportBatch, UserImportBatchLog
from app.modules.system.schemas.user_transfer import UserImportRecord
from app.modules.system.service import authorization_import_candidate as module
from app.modules.system.service import user_import_service
from app.modules.system.service.user_import_service import (
    _compute_file_sha256,
    _compute_records_hash,
)
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)
from tests.committed_database import isolated_migrated_schema

FILE = b"validated import workbook"
ARTIFACT = ArtifactExecution(0, "import-operation", "owner-1")


class Storage:
    def __init__(self, failure=None):
        self.writes = []
        self.failure = failure

    async def save_artifact(self, data, *, identity):
        self.writes.append((data, identity))
        if self.failure is not None:
            raise self.failure
        return identity.storage_key


@pytest.fixture
async def import_sessions():
    async with isolated_migrated_schema(engine.url) as scoped:
        plain = async_sessionmaker(scoped, expire_on_commit=False, autoflush=False)
        managed = async_sessionmaker(
            scoped,
            class_=AuthorizationPlanSession,
            expire_on_commit=False,
            autoflush=False,
        )
        async with plain() as db:
            code = await db.scalar(
                select(Tenant.tenant_code).where(Tenant.tenant_id == 0)
            )
            await db.execute(
                insert(User).values(
                    tenant_id=0,
                    user_id=101,
                    user_name="importer",
                    hashed_password="fixture",
                    status="1",
                )
            )
            for rid, role_code, scope in ((201, "R_SUPER", "1"), (202, "R_USER", "5")):
                await db.execute(
                    insert(Role).values(
                        tenant_id=0,
                        role_id=rid,
                        role_name=role_code,
                        role_code=role_code,
                        data_scope=scope,
                        status="1",
                    )
                )
            await db.execute(
                insert(user_roles).values(tenant_id=0, user_id=101, role_id=201)
            )
            await db.execute(
                insert(Dept).values(
                    tenant_id=0,
                    dept_id=301,
                    dept_name="Team",
                    ancestors="0",
                    status="1",
                )
            )
            for mid, permission in enumerate(
                (
                    "system:user:import",
                    "system:dept:list",
                    "system:user:role-auth",
                ),
                401,
            ):
                await db.execute(
                    insert(Menu).values(
                        tenant_id=0,
                        menu_id=mid,
                        menu_name=permission,
                        permission=permission,
                        menu_type="F",
                        status="1",
                    )
                )
                await db.execute(
                    insert(role_menus).values(tenant_id=0, role_id=201, menu_id=mid)
                )
            await db.execute(
                insert(SystemSetting).values(
                    tenant_id=0,
                    setting_id=501,
                    setting_key="auth:default_password",
                    setting_value="StrongTest123!",
                    status="1",
                )
            )
            await db.execute(
                update(UserFieldPolicyState)
                .where(
                    UserFieldPolicyState.tenant_id == 0,
                )
                .values(epoch=2, mode="ready", candidate_manifest_digest="a" * 64)
            )
            await db.commit()
        yield managed, plain, TenantContext(0, code, 101, 1, "access_token")


def record(number=2, name="newuser", **values):
    return UserImportRecord(row_num=number, user_name=name, dept_input="Team", **values)


async def preview(plain, records, *, on_conflict="skip"):
    async with plain() as db:
        await db.execute(
            insert(UserImportBatch).values(
                tenant_id=0,
                batch_id="batch-1",
                operator_id=101,
                filename="users.xlsx",
                file_sha256=_compute_file_sha256(FILE),
                records_hash=_compute_records_hash(records),
                total_rows=len(records),
                preview_token="preview-1",
                reason="staff import",
                on_conflict=on_conflict,
                status=ImportBatchStatus.PREVIEW_DONE,
            )
        )
        await db.commit()


async def existing(plain, **values):
    values.setdefault("user_gender", "0")
    async with plain() as db:
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=102,
                user_name="existing",
                hashed_password="unchanged",
                status="1",
                **values,
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=102, role_id=202)
        )
        await db.execute(
            insert(user_depts).values(
                tenant_id=0, user_id=102, dept_id=301, is_primary="Y"
            )
        )
        await db.commit()


async def run(db, tenant, records, **kwargs):
    execute = (
        system_candidate_owner.import_users
        if kwargs.pop("owner", False)
        else module.import_authorization_candidate.execute
    )
    return await execute(
        db,
        tenant=tenant,
        actor_user_version=1,
        records=records,
        file_bytes=FILE,
        preview_token="preview-1",
        filename="users.xlsx",
        reason="staff import",
        on_conflict=kwargs.pop("on_conflict", "skip"),
        sync_mode=kwargs.pop("sync_mode", EmployeeNoSyncMode.CREATE_ONLY),
        has_role_column=False,
        file_storage=kwargs.pop("file_storage", Storage()),
        artifact_execution=kwargs.pop("artifact_execution", ARTIFACT),
        **kwargs,
    )


async def state(plain):
    async with plain() as db:
        return (
            await db.execute(
                select(
                    UserFieldPolicyState.epoch,
                    UserFieldPolicyState.mode,
                ).where(UserFieldPolicyState.tenant_id == 0)
            )
        ).one()


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "partial",
        "all_failed",
        "fail_fast",
        "skip",
        "noop",
        "overwrite",
        "full_sync",
    ],
)
async def test_import_outcomes_epoch_and_replay(import_sessions, scenario):
    managed, plain, tenant = import_sessions
    if scenario in {"skip", "noop", "overwrite", "full_sync"}:
        await existing(plain, employee_no="E1" if scenario == "full_sync" else None)
        rows = [
            record(
                name="renamed" if scenario == "full_sync" else "existing",
                nickname="changed" if scenario in {"overwrite", "full_sync"} else None,
                employee_no="E1" if scenario == "full_sync" else None,
            )
        ]
    elif scenario in {"partial", "all_failed", "fail_fast"}:
        rows = [record(), record(3, "newuser")]
        if scenario == "all_failed":
            rows = [record().model_copy(update={"dept_input": "absent"})]
    else:
        rows = [record()]
    conflict = (
        "fail_fast"
        if scenario == "fail_fast"
        else "overwrite"
        if scenario in {"noop", "overwrite"}
        else "skip"
    )
    await preview(plain, rows, on_conflict=conflict)
    storage = Storage()
    async with managed() as db:
        result = await run(
            db,
            tenant,
            rows,
            on_conflict=conflict,
            file_storage=storage,
            sync_mode=EmployeeNoSyncMode.FULL_SYNC
            if scenario == "full_sync"
            else EmployeeNoSyncMode.CREATE_ONLY,
        )
        await db.commit()
    expected = {
        "success": ("SUCCESS", 1, 0, 0, 0, 3),
        "partial": ("PARTIAL_SUCCESS", 1, 0, 0, 1, 3),
        "all_failed": ("FAILED", 0, 0, 0, 1, 2),
        "fail_fast": ("FAILED", 0, 0, 0, 2, 2),
        "skip": ("SUCCESS", 0, 1, 0, 0, 2),
        "noop": ("SUCCESS", 0, 0, 1, 0, 2),
        "overwrite": ("SUCCESS", 0, 0, 1, 0, 3),
        "full_sync": ("SUCCESS", 0, 0, 1, 0, 3),
    }[scenario]
    assert (
        result.status,
        result.success_count,
        result.skipped_count,
        result.overwritten_count,
        result.failed_count,
    ) == expected[:5]
    assert await state(plain) == (
        expected[5],
        "ready" if expected[5] == 2 else "legacy",
    )
    assert len(storage.writes) == bool(result.failed_count)
    async with plain() as db:
        assert (
            await db.scalars(
                select(UserImportBatchLog.event).order_by(UserImportBatchLog.log_id)
            )
        ).all()[0] == "EXECUTE_START"
        if scenario in {"noop", "overwrite", "full_sync"}:
            assert (
                await db.scalar(select(User.hashed_password).where(User.user_id == 102))
                == "unchanged"
            )
        if scenario == "full_sync":
            assert (
                await db.execute(
                    select(User.user_name, User.auth_version).where(User.user_id == 102)
                )
            ).one() == ("renamed", 2)
    if result.status != "FAILED":
        async with managed() as db:
            replay = await run(
                db,
                tenant,
                rows,
                on_conflict=conflict,
                file_storage=storage,
                sync_mode=EmployeeNoSyncMode.FULL_SYNC
                if scenario == "full_sync"
                else EmployeeNoSyncMode.CREATE_ONLY,
            )
            await db.commit()
        assert replay.idempotent_replay
        assert await state(plain) == (
            expected[5],
            "ready" if expected[5] == 2 else "legacy",
        )


async def test_complete_plan_before_batch_cas_and_all_logs_before_file(import_sessions):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3)]
    await preview(plain, rows)
    sql = []

    def observe(_c, _cur, statement, _params, _ctx, _many):
        sql.append(statement)

    class ObservedStorage(Storage):
        async def save_artifact(self, data, *, identity):
            sql.append("FILE")
            return await super().save_artifact(data, identity=identity)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    try:
        async with managed() as db:
            await run(db, tenant, rows, file_storage=ObservedStorage())
            await db.commit()
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    cas = next(
        i for i, s in enumerate(sql) if s.startswith("UPDATE sys_user_import_batch ")
    )
    for table in (
        "sys_user_field_policy_global_guard",
        "sys_user_field_policy_state",
        "sys_role",
        "sys_dept",
        "sys_user",
        "sys_setting",
        "sys_user_import_batch",
    ):
        assert any(table in s and "FOR " in s for s in sql[:cas]), table
    after_file = sql[sql.index("FILE") + 1 :]
    assert not any(s.startswith(("INSERT", "DELETE")) for s in after_file)
    assert all(
        "sys_user_import_batch" in s or "sys_user_field_policy_state" in s
        for s in after_file
        if s.startswith("UPDATE")
    )


@pytest.mark.parametrize("failure", [OSError("disk"), asyncio.CancelledError()])
async def test_file_failure_or_cancel_rolls_back_everything(import_sessions, failure):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3)]
    await preview(plain, rows)
    async with managed() as db:
        with pytest.raises(type(failure)):
            await run(db, tenant, rows, file_storage=Storage(failure))
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    assert await state(plain) == (2, "ready")
    async with plain() as db:
        assert (
            await db.scalar(select(User.user_id).where(User.user_name == "newuser"))
            is None
        )
        assert (
            await db.scalar(select(UserImportBatch.status))
            == ImportBatchStatus.PREVIEW_DONE
        )
        assert (await db.scalars(select(UserImportBatchLog.log_id))).all() == []


async def test_hash_precedes_g_and_setting_change_fails_closed(
    import_sessions, monkeypatch
):
    managed, plain, tenant = import_sessions
    rows = [record()]
    await preview(plain, rows)

    async def hash_then_change(_password):
        async with plain() as other:
            await other.execute(
                update(SystemSetting)
                .where(SystemSetting.setting_id == 501)
                .values(setting_value="OtherStrong123!")
            )
            await other.commit()
        return "stale-hash"

    monkeypatch.setattr(module, "hash_password", hash_then_change)
    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            await run(db, tenant, rows)
        assert caught.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
        await db.rollback()
    assert await state(plain) == (2, "ready")


@pytest.mark.parametrize(
    "artifact",
    [
        None,
        ArtifactExecution(1, "other", "owner"),
        ArtifactExecution(0, "../unsafe", "owner"),
    ],
)
async def test_artifact_execution_must_be_trusted_shape_and_same_tenant(
    import_sessions, artifact
):
    managed, plain, tenant = import_sessions
    rows = [record()]
    await preview(plain, rows)
    async with managed() as db:
        with pytest.raises(BusinessException):
            await run(db, tenant, rows, artifact_execution=artifact)
        await db.rollback()
    assert await state(plain) == (2, "ready")


@pytest.mark.parametrize(
    "field,value",
    [("user_name", "bad-name"), ("user_email", "invalid"), ("user_phone", "invalid")],
)
async def test_malformed_direct_record_cannot_bypass_parser(
    import_sessions, field, value
):
    managed, plain, tenant = import_sessions
    rows = [record().model_copy(update={field: value})]
    await preview(plain, rows)
    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            await run(db, tenant, rows)
        assert (
            caught.value.error_code
            == {
                "user_name": "AI_IMPORT_USERNAME_INVALID",
                "user_email": "AI_IMPORT_EMAIL_INVALID",
                "user_phone": "AI_IMPORT_PHONE_INVALID",
            }[field]
        )
        await db.rollback()
    assert await state(plain) == (2, "ready")


async def test_hash_runs_after_read_transaction_is_released(
    import_sessions, monkeypatch
):
    managed, plain, tenant = import_sessions
    rows = [record()]
    await preview(plain, rows)
    async with managed() as db:

        async def check_hash(_password):
            assert not db.in_transaction(), (
                "password worker retained the read connection"
            )
            return "test-hash"

        monkeypatch.setattr(module, "hash_password", check_hash)
        await run(db, tenant, rows)
        await db.commit()


@pytest.mark.parametrize(
    "failure",
    [
        "AUTHORIZATION_SNAPSHOT_STALE",
        "FIELD_POLICY_LOCK_TIMEOUT",
        "AI_IMPORT_UNKNOWN_INTEGRITY_ERROR",
        None,
    ],
)
async def test_fatal_row_error_is_never_a_partial_success(
    import_sessions, monkeypatch, failure
):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3, "second")]
    await preview(plain, rows)
    original = module.import_authorization_candidate._row

    async def fail_second(tx, tid, kind, row, *args):
        if row.row_num == 3:
            raise module.BusinessRuleException("fatal", error_code=failure)
        return await original(tx, tid, kind, row, *args)

    monkeypatch.setattr(module.import_authorization_candidate, "_row", fail_second)
    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            await run(db, tenant, rows)
        assert caught.value.error_code == failure
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    assert await state(plain) == (2, "ready")
    async with plain() as db:
        assert (
            await db.scalar(select(User.user_id).where(User.user_name == "newuser"))
            is None
        )
        assert (
            await db.scalar(select(UserImportBatch.status))
            == ImportBatchStatus.PREVIEW_DONE
        )


async def test_release_failure_never_commits_row_counts(import_sessions):
    managed, plain, tenant = import_sessions
    rows = [record()]
    await preview(plain, rows)
    releases = 0

    def fail_chunk_release(_c, _cur, statement, _params, _ctx, _many):
        nonlocal releases
        if statement.startswith("RELEASE SAVEPOINT"):
            releases += 1
            if releases == 2:
                raise RuntimeError("chunk release failed")

    event.listen(
        plain.kw["bind"].sync_engine, "before_cursor_execute", fail_chunk_release
    )
    try:
        async with managed() as db:
            with pytest.raises(RuntimeError, match="chunk release failed"):
                await run(db, tenant, rows)
            with pytest.raises(BusinessException):
                await db.commit()
            await db.rollback()
    finally:
        event.remove(
            plain.kw["bind"].sync_engine, "before_cursor_execute", fail_chunk_release
        )
    assert await state(plain) == (2, "ready")
    async with plain() as db:
        assert (
            await db.execute(
                select(UserImportBatch.status, UserImportBatch.success_count)
            )
        ).one() == (ImportBatchStatus.PREVIEW_DONE, 0)
        assert (
            await db.scalar(select(User.user_id).where(User.user_name == "newuser"))
            is None
        )


async def test_concurrent_execute_serializes_then_replays_without_second_epoch(
    import_sessions,
):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3)]
    await preview(plain, rows)
    at_file, release_file, second_barrier = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    attempts = 0

    class WaitingStorage(Storage):
        async def save_artifact(self, data, *, identity):
            at_file.set()
            await release_file.wait()
            return await super().save_artifact(data, identity=identity)

    def observe(_c, _cur, statement, _params, _ctx, _many):
        nonlocal attempts
        if (
            "FROM sys_user_field_policy_state" in statement
            and "FOR UPDATE" in statement
        ):
            attempts += 1
            if attempts == 2:
                second_barrier.set()

    async def worker(storage):
        async with managed() as db:
            try:
                result = await run(db, tenant, rows, file_storage=storage)
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    first = asyncio.create_task(worker(WaitingStorage()))
    second = None
    try:
        await asyncio.wait_for(at_file.wait(), 5)
        second = asyncio.create_task(worker(Storage()))
        await asyncio.wait_for(second_barrier.wait(), 2)
        assert not second.done()
        release_file.set()
        a, b = await asyncio.gather(first, second)
        assert not a.idempotent_replay and b.idempotent_replay
        assert await state(plain) == (3, "legacy")
    finally:
        release_file.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (first, second) if t is not None), return_exceptions=True
        )
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)


async def test_cancel_waiting_file_rolls_back_and_releases_barrier(import_sessions):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3)]
    await preview(plain, rows)
    at_file = asyncio.Event()

    class WaitingStorage(Storage):
        async def save_artifact(self, data, *, identity):
            self.writes.append((data, identity))
            at_file.set()
            await asyncio.Event().wait()

    async def worker():
        async with managed() as db:
            try:
                await run(db, tenant, rows, file_storage=WaitingStorage())
            except BaseException:
                await db.rollback()
                raise

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(at_file.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await state(plain) == (2, "ready")
    async with managed() as db:
        result = await run(db, tenant, rows)
        await db.commit()
    assert result.success_count == 1 and await state(plain) == (3, "legacy")


@pytest.mark.parametrize(
    "code",
    ["AUTHORIZATION_SNAPSHOT_STALE", "FIELD_POLICY_LOCK_TIMEOUT", "UNKNOWN_FATAL"],
)
async def test_classifier_stale_authorization_is_fatal(
    import_sessions, monkeypatch, code
):
    managed, plain, tenant = import_sessions
    rows = [record()]
    await preview(plain, rows)

    async def stale(*_args, **_kwargs):
        raise module.BusinessRuleException("stale", error_code=code)

    monkeypatch.setattr(
        user_role_assignment_service, "validate_import_role_assignment", stale
    )
    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            await run(db, tenant, rows)
        assert caught.value.error_code == code
        await db.rollback()
    assert await state(plain) == (2, "ready")


async def test_import_classification_is_read_only_including_orm_state(import_sessions):
    _, plain, tenant = import_sessions
    rows = [record()]
    async with plain() as db:
        actor = await db.scalar(select(User).where(User.user_id == 101))
        classified = await module._classify_records(
            db,
            rows,
            actor,
            has_role_column=False,
            strict_authorization=True,
            tenant=tenant,
        )
        assert len(classified[0]) == 1
        assert not db.new and not db.dirty and not db.deleted


async def test_owner_confirms_commit_before_return(import_sessions):
    managed, plain, tenant = import_sessions
    rows = [record()]
    await preview(plain, rows)
    async with managed() as db:
        result = await run(db, tenant, rows, owner=True)
        assert not db.in_transaction()
        assert result.success_count == 1
        assert await state(plain) == (3, "legacy")
        async with plain() as observer:
            assert (
                await observer.scalar(
                    select(User.user_id).where(User.user_name == "newuser")
                )
                is not None
            )


async def test_owner_file_failure_rolls_back_without_deleting_owned_bytes(
    import_sessions,
):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3)]
    await preview(plain, rows)
    storage = Storage(OSError("save acknowledgement unavailable"))
    async with managed() as db:
        with pytest.raises(OSError):
            await run(db, tenant, rows, file_storage=storage, owner=True)
        assert not db.in_transaction()
    assert len(storage.writes) == 1
    assert await state(plain) == (2, "ready")
    async with plain() as db:
        assert (
            await db.scalar(select(UserImportBatch.status))
            == ImportBatchStatus.PREVIEW_DONE
        )
        assert (
            await db.scalar(select(User.user_id).where(User.user_name == "newuser"))
            is None
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("role_input", ",".join(["R_USER"] * 257)),
        ("role_input", "a" * (65536 + 1)),
        ("dept_input", "/".join(["Team"] * 257)),
        ("dept_input", "a" * (8192 + 1)),
    ],
    ids=["role_tokens", "role_bytes", "dept_segments", "dept_bytes"],
)
async def test_reference_input_budget_precedes_database_access(
    import_sessions, monkeypatch, field, value
):
    managed, _, tenant = import_sessions
    rows = [record().model_copy(update={field: value})]

    async def forbidden_read(*_args, **_kwargs):
        pytest.fail("over-budget reference reached database")

    monkeypatch.setattr(module.import_authorization_candidate, "_batch", forbidden_read)
    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            await run(db, tenant, rows)
        assert caught.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"


async def test_normalization_keeps_original_preview_hash(import_sessions):
    managed, plain, tenant = import_sessions
    rows = [record(nickname=" Staff ").model_copy(update={"dept_input": " Team "})]
    await preview(plain, rows)
    async with managed() as db:
        await run(db, tenant, rows, owner=True)
    async with plain() as db:
        assert (
            await db.scalar(select(User.nickname).where(User.user_name == "newuser"))
            == "Staff"
        )


async def test_storage_wrong_owned_key_cannot_be_bound_to_batch(import_sessions):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3)]
    await preview(plain, rows)

    class WrongStorage(Storage):
        async def save_artifact(self, data, *, identity):
            await super().save_artifact(data, identity=identity)
            return "another-owner/payload"

    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            await run(db, tenant, rows, owner=True, file_storage=WrongStorage())
        assert caught.value.error_code == "AI_ARTIFACT_UNVERIFIED"
    assert await state(plain) == (2, "ready")


async def test_real_local_owned_artifact_preserves_identity(import_sessions, tmp_path):
    managed, plain, tenant = import_sessions
    rows = [record(), record(3)]
    await preview(plain, rows)
    storage = LocalFileStorage(tmp_path / "owned-import")
    async with managed() as db:
        result = await run(db, tenant, rows, owner=True, file_storage=storage)
    identity = ARTIFACT.identity("import_errors", "batch-1")
    assert result.failed_rows_file == identity.storage_key
    assert await storage.artifact_identity(result.failed_rows_file) == identity
    data = await storage.read_artifact(result.failed_rows_file, identity=identity)
    assert data[:2] == b"PK"
    assert result.failed_count == 1 and await state(plain) == (3, "legacy")


@pytest.mark.parametrize("resolver", ["resolve_dept", "resolve_role_input"])
async def test_reference_resolver_unknown_failure_is_fatal(
    import_sessions, monkeypatch, resolver
):
    managed, plain, tenant = import_sessions
    rows = [record(role_input="R_USER")]
    await preview(plain, rows)

    async def fail(*_args, **_kwargs):
        raise module.BusinessRuleException("unknown", error_code="UNKNOWN_FATAL")

    monkeypatch.setattr(user_import_service, resolver, fail)
    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            await run(db, tenant, rows)
        assert caught.value.error_code == "UNKNOWN_FATAL"
        await db.rollback()


async def test_prepare_cancellation_is_not_replaced_by_cleanup_failure(
    import_sessions, monkeypatch
):
    managed, _, tenant = import_sessions

    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    async def cleanup_unknown(_db):
        raise module.BusinessRuleException(
            "unknown", error_code="AUTHORIZATION_ROLLBACK_UNKNOWN"
        )

    monkeypatch.setattr(module.import_authorization_candidate, "_batch", cancel)
    monkeypatch.setattr(module, "rollback_authorization_session", cleanup_unknown)
    async with managed() as db:
        with pytest.raises(asyncio.CancelledError):
            await run(db, tenant, [record()])


async def test_null_gender_to_import_default_is_a_real_mutation(import_sessions):
    managed, plain, tenant = import_sessions
    await existing(plain, user_gender=None)
    rows = [record(name="existing")]
    await preview(plain, rows, on_conflict="overwrite")
    changes = []

    def observe(_c, _cur, statement, parameters, _ctx, _many):
        if statement.startswith("UPDATE sys_user "):
            changes.append((statement, parameters))

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    try:
        async with managed() as db:
            await run(db, tenant, rows, on_conflict="overwrite", owner=True)
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    assert len(changes) == 1
    assert "user_gender=" in changes[0][0] and "0" in changes[0][1]
    async with plain() as db:
        assert (
            await db.scalar(select(User.user_gender).where(User.user_id == 102)) == "0"
        )
    assert await state(plain) == (3, "legacy")
