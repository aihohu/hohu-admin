"""batch_create_users_from_records 单测（Task 10，spec §3.6 line 2068-2097）。

覆盖：
- preview_token 三重校验（spec §2.19 line 575-577）
- CAS PREVIEW_DONE → RUNNING（spec §2.27 line 1133-1216）
- chunk + savepoint 落库（spec §2.20 line 592-622）
- IntegrityError 区分 user_name UNIQUE → AI_IMPORT_USERNAME_DUPLICATE（spec §2.25）
- on_conflict skip / overwrite / fail_fast（spec §2.21）
- batch_log 写 EXECUTE_START / CHUNK_PROGRESS / EXECUTE_FINISH（spec §2.28）
- failed_rows_file 文件化（spec §3.3 line 1700-1717）
- 状态转 SUCCESS / PARTIAL_SUCCESS / FAILED（spec §2.26）

测试通过 dry_run_import_users 走完整 preview 流程建 PREVIEW_DONE batch，
再调 batch_create_users_from_records 验证 execute 阶段语义。
"""

import pytest
from fakeredis import aioredis as fakeredis_async
from sqlalchemy import select as _select

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_ALL,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core import redis as redis_module
from app.core.exceptions import BusinessRuleException
from app.core.file_storage import MockFileStorage
from app.core.security import verify_password
from app.modules.system.models.config import Config
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.user.constants import (
    FAILED_ROWS_PREVIEW_LIMIT,
    ImportBatchStatus,
)
from app.modules.system.user.import_service import (
    batch_create_users_from_records,
    dry_run_import_users,
)
from app.modules.system.user.models import UserImportBatch, UserImportBatchLog
from app.modules.system.user.schemas import UserImportRecord

# ========== helpers ==========


def _make_dept(
    dept_id: int,
    name: str,
    parent_id: int | None = None,
    ancestors: str = "0",
) -> Dept:
    return Dept(
        dept_id=dept_id,
        parent_id=parent_id,
        ancestors=ancestors,
        dept_name=name,
        order_num=0,
        status="1",
    )


def _make_role(
    role_id: int,
    code: str,
    name: str,
    data_scope: str = DATA_SCOPE_ALL,
    status: str = "1",
) -> Role:
    return Role(
        role_id=role_id,
        role_code=code,
        role_name=name,
        data_scope=data_scope,
        status=status,
    )


def _make_user(
    user_id: int,
    user_name: str,
    roles: list[Role],
    depts: list[Dept] | None = None,
    status: str = "1",
) -> User:
    return User(
        user_id=user_id,
        user_name=user_name,
        hashed_password="x",
        status=status,
        roles=roles,
        depts=depts or [],
    )


def _make_record(
    row_num: int,
    user_name: str,
    *,
    dept_input: str = "QA-Exec-Dept",
    role_input: str | None = None,
    employee_no: str | None = None,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
) -> UserImportRecord:
    return UserImportRecord(
        row_num=row_num,
        user_name=user_name,
        employee_no=employee_no,
        nickname=nickname,
        user_email=user_email,
        user_phone=user_phone,
        dept_input=dept_input,
        role_input=role_input,
    )


_FILE_BYTES = b"execute-test-file-content-v10"


@pytest.fixture(autouse=True)
async def fake_redis(db_session, monkeypatch):  # noqa: ARG001
    """autouse：在 db_session reset 之后替换 redis_client 为 fakeredis。"""
    redis = fakeredis_async.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "redis_client", redis)
    try:
        yield redis
    finally:
        await redis.flushall()
        await redis.aclose()


@pytest.fixture
def file_storage() -> MockFileStorage:
    """每个测试独立 MockFileStorage（spec §3.9 注入）。"""
    return MockFileStorage()


async def _seed_default_password(
    db_session,
    password: str = "QA-Default-Pwd-123",
) -> None:
    """设置 sys_config.auth:default_password（spec §2.5）。"""
    db_session.add(
        Config(
            config_id=999_001,
            config_name="默认密码",
            config_key="auth:default_password",
            config_value=password,
            config_type="text",
            config_group="auth",
            status="1",
        )
    )
    await db_session.flush()


async def _setup_preview(
    db_session,
    records,
    operator,
    *,
    file_bytes: bytes = _FILE_BYTES,
    reason: str = "QA execute test",
    on_conflict: str = "skip",
) -> UserImportBatch:
    """走完整 dry_run 流程，返回 PREVIEW_DONE 状态的 batch 行。"""
    await _seed_default_password(db_session)
    _result, batch = await dry_run_import_users(
        db_session,
        records,
        operator,
        file_bytes=file_bytes,
        filename="test.xlsx",
        reason=reason,
        on_conflict=on_conflict,
    )
    await db_session.flush()
    return batch


async def _fetch_super_role(db_session) -> Role:
    return (
        await db_session.execute(
            _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
        )
    ).scalar_one()


async def _count_users_by_prefix(db_session, prefix: str) -> int:
    rows = (
        (
            await db_session.execute(
                _select(User).where(User.user_name.like(f"{prefix}%"))
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


# ========== Triple Validation（spec §2.19 line 575-577） ==========


class TestTripleValidation:
    """preview_token + file_sha256 + records_hash + operator_id 四重一致性校验。"""

    async def test_execute_with_valid_token_creates_users(
        self, db_session, file_storage
    ):
        """spec 用例 1：合法 token + 匹配 → 用户创建 + status=SUCCESS。"""
        dept = _make_dept(8101, "QA-Exec-Dept")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9301, "QA_EXEC_OK", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_EXEC_U1", dept_input="QA-Exec-Dept"),
            _make_record(3, "QA_EXEC_U2", dept_input="QA-Exec-Dept"),
        ]
        batch = await _setup_preview(db_session, records, operator)

        result = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        assert result.success_count == 2
        assert result.failed_count == 0
        assert result.batch_id == batch.batch_id
        assert result.idempotent_replay is False
        # 实际入库 2 个用户
        assert await _count_users_by_prefix(db_session, "QA_EXEC_U") == 2

    async def test_execute_with_invalid_token_rejected(self, db_session, file_storage):
        """preview_token 不存在 → AI_IMPORT_PREVIEW_INVALID。"""
        dept = _make_dept(8102, "QA-Exec-Dept-Inv")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9302, "QA_EXEC_INV", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()
        await _seed_default_password(db_session)

        with pytest.raises(BusinessRuleException) as exc:
            await batch_create_users_from_records(
                db_session,
                [_make_record(2, "QA_X", dept_input="QA-Exec-Dept-Inv")],
                preview_token="nonexistent-token",
                file_bytes=_FILE_BYTES,
                filename="test.xlsx",
                reason="QA execute test",
                current_user=operator,
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_IMPORT_PREVIEW_INVALID"

    async def test_execute_with_changed_file_bytes_rejected(
        self, db_session, file_storage
    ):
        """file_bytes 与 dry_run 时不一致（file_sha256 mismatch）→ AI_IMPORT_PREVIEW_INVALID。"""
        dept = _make_dept(8103, "QA-Exec-Dept-F")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9303, "QA_EXEC_F", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_EXEC_F1", dept_input="QA-Exec-Dept-F")]
        batch = await _setup_preview(db_session, records, operator)

        with pytest.raises(BusinessRuleException) as exc:
            await batch_create_users_from_records(
                db_session,
                records,
                preview_token=batch.preview_token,
                file_bytes=b"different-bytes-tampered",  # 不一致
                filename="test.xlsx",
                reason="QA execute test",
                current_user=operator,
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_IMPORT_PREVIEW_INVALID"

    async def test_execute_with_changed_records_rejected(
        self, db_session, file_storage
    ):
        """records 与 dry_run 时不一致（records_hash mismatch）→ AI_IMPORT_PREVIEW_INVALID。"""
        dept = _make_dept(8104, "QA-Exec-Dept-R")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9304, "QA_EXEC_R", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_EXEC_R1", dept_input="QA-Exec-Dept-R")]
        batch = await _setup_preview(db_session, records, operator)

        # 改 record 的 nickname → records_hash 变
        tampered = [
            _make_record(
                2,
                "QA_EXEC_R1",
                dept_input="QA-Exec-Dept-R",
                nickname="tampered",
            )
        ]
        with pytest.raises(BusinessRuleException) as exc:
            await batch_create_users_from_records(
                db_session,
                tampered,
                preview_token=batch.preview_token,
                file_bytes=_FILE_BYTES,
                filename="test.xlsx",
                reason="QA execute test",
                current_user=operator,
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_IMPORT_PREVIEW_INVALID"

    async def test_execute_with_different_operator_rejected(
        self, db_session, file_storage
    ):
        """operator 不是 dry_run 那个人 → AI_IMPORT_PREVIEW_INVALID。"""
        dept = _make_dept(8105, "QA-Exec-Dept-O")
        super_role = await _fetch_super_role(db_session)
        operator_a = _make_user(9305, "QA_EXEC_OP_A", [super_role], [dept])
        operator_b = _make_user(9306, "QA_EXEC_OP_B", [super_role], [dept])
        db_session.add_all([dept, operator_a, operator_b])
        await db_session.flush()

        records = [_make_record(2, "QA_EXEC_OP_X", dept_input="QA-Exec-Dept-O")]
        batch = await _setup_preview(db_session, records, operator_a)

        with pytest.raises(BusinessRuleException) as exc:
            await batch_create_users_from_records(
                db_session,
                records,
                preview_token=batch.preview_token,
                file_bytes=_FILE_BYTES,
                filename="test.xlsx",
                reason="QA execute test",
                current_user=operator_b,  # 不同 operator
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_IMPORT_PREVIEW_INVALID"


# ========== Idempotency（spec §2.27 line 1133-1216） ==========


class TestExecuteIdempotency:
    """preview_token 只能 execute 一次（CAS PREVIEW_DONE → RUNNING）。"""

    async def test_execute_same_token_twice_success_replay(
        self, db_session, file_storage
    ):
        """spec line 1205：SUCCESS 后重放返回 idempotent_replay=True + 原结果。"""
        dept = _make_dept(8201, "QA-Exec-Dept-RP")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9401, "QA_EXEC_RP", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_RP_U1", dept_input="QA-Exec-Dept-RP")]
        batch = await _setup_preview(db_session, records, operator)

        first = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )
        assert first.idempotent_replay is False
        assert first.success_count == 1

        # 第二次重放（batch 已 SUCCESS）
        second = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )
        assert second.idempotent_replay is True
        assert second.success_count == first.success_count
        assert second.batch_id == first.batch_id
        # 没多创建用户（仍是 1 个）
        assert await _count_users_by_prefix(db_session, "QA_RP_U") == 1

    async def test_execute_failed_batch_rejected(self, db_session, file_storage):
        """spec line 1209：FAILED 状态重放 → AI_IMPORT_ALREADY_EXECUTED。"""
        dept = _make_dept(8202, "QA-Exec-Dept-FL")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9402, "QA_EXEC_FL", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_FL_U1", dept_input="QA-Exec-Dept-FL")]
        batch = await _setup_preview(db_session, records, operator)

        # 手动转 FAILED（模拟执行失败后重放）
        batch.status = ImportBatchStatus.FAILED
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await batch_create_users_from_records(
                db_session,
                records,
                preview_token=batch.preview_token,
                file_bytes=_FILE_BYTES,
                filename="test.xlsx",
                reason="QA execute test",
                current_user=operator,
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_IMPORT_ALREADY_EXECUTED"

    async def test_execute_cancelled_batch_rejected(self, db_session, file_storage):
        """spec line 1210：CANCELLED 状态重放 → AI_IMPORT_ALREADY_EXECUTED。"""
        dept = _make_dept(8203, "QA-Exec-Dept-CC")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9403, "QA_EXEC_CC", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_CC_U1", dept_input="QA-Exec-Dept-CC")]
        batch = await _setup_preview(db_session, records, operator)

        batch.status = ImportBatchStatus.CANCELLED
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await batch_create_users_from_records(
                db_session,
                records,
                preview_token=batch.preview_token,
                file_bytes=_FILE_BYTES,
                filename="test.xlsx",
                reason="QA execute test",
                current_user=operator,
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_IMPORT_ALREADY_EXECUTED"


# ========== Chunk + Savepoint（spec §2.20） ==========


class TestChunkSavepoint:
    """chunk 100 rows + 行级 savepoint + IntegrityError 区分。"""

    async def test_execute_creates_users_with_default_password(
        self, db_session, file_storage
    ):
        """spec §2.5：新用户用 sys_config.auth:default_password 哈希入库。"""
        dept = _make_dept(8301, "QA-Exec-Dept-PWD")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9501, "QA_EXEC_PWD", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_PWD_U1", dept_input="QA-Exec-Dept-PWD")]
        batch = await _setup_preview(db_session, records, operator)

        # 改 sys_config.auth:default_password 为可识别值
        config_row = (
            await db_session.execute(
                _select(Config).where(Config.config_key == "auth:default_password")
            )
        ).scalar_one()
        config_row.config_value = "MyInitPwd-999"
        await db_session.flush()

        await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        created = (
            await db_session.execute(_select(User).where(User.user_name == "QA_PWD_U1"))
        ).scalar_one()
        assert verify_password("MyInitPwd-999", created.hashed_password)

    async def test_execute_username_duplicate_in_batch(self, db_session, file_storage):
        """spec §2.25：同 batch 内两条记录 user_name 相同 → 第二条进 failed_rows。

        dry_run 阶段两条都不在 DB → 都归 new_records；
        execute 阶段第一条 INSERT 成功，第二条命中 UNIQUE 约束 → IntegrityError
        → service 层捕获并转换为 AI_IMPORT_USERNAME_DUPLICATE。
        """
        dept = _make_dept(8302, "QA-Exec-Dept-DUP")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9502, "QA_EXEC_DUP", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_DUP_SAME", dept_input="QA-Exec-Dept-DUP"),
            _make_record(3, "QA_DUP_SAME", dept_input="QA-Exec-Dept-DUP"),
        ]
        batch = await _setup_preview(db_session, records, operator)

        result = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        assert result.success_count == 1
        assert result.failed_count == 1
        assert (
            result.failed_rows_preview[0].error_code == "AI_IMPORT_USERNAME_DUPLICATE"
        )
        assert result.failed_rows_preview[0].row_num == 3

    async def test_execute_writes_failed_rows_file_when_failures(
        self, db_session, file_storage
    ):
        """spec §3.3 line 1700：失败行写 Excel 文件，路径存 batch.failed_rows_file。"""
        dept = _make_dept(8303, "QA-Exec-Dept-FF")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9503, "QA_EXEC_FF", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_FF_OK", dept_input="QA-Exec-Dept-FF"),
            _make_record(3, "QA_FF_DUP", dept_input="QA-Exec-Dept-FF"),
            _make_record(4, "QA_FF_DUP", dept_input="QA-Exec-Dept-FF"),  # 同名冲突
        ]
        batch = await _setup_preview(db_session, records, operator)

        result = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        assert result.failed_count == 1
        assert result.failed_rows_file is not None
        assert "import-error" in result.failed_rows_file
        # 文件实际写入 MockFileStorage
        assert await file_storage.exists(result.failed_rows_file)
        # batch 行也存了路径
        db_batch = (
            await db_session.execute(
                _select(UserImportBatch).where(
                    UserImportBatch.batch_id == batch.batch_id
                )
            )
        ).scalar_one()
        assert db_batch.failed_rows_file == result.failed_rows_file

    async def test_execute_failed_rows_preview_capped_at_20(
        self, db_session, file_storage
    ):
        """spec §3.3 line 1702：failed_rows_preview 仅前 20 条（toast 用）。"""
        dept = _make_dept(8304, "QA-Exec-Dept-PV")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9504, "QA_EXEC_PV", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        # 25 条同 user_name → 第 1 条成功，第 2-25 条都 USERNAME_DUPLICATE
        records = [
            _make_record(i, "QA_PV_DUP", dept_input="QA-Exec-Dept-PV")
            for i in range(2, 27)
        ]
        batch = await _setup_preview(db_session, records, operator)

        result = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        assert result.failed_count == 24
        assert len(result.failed_rows_preview) == FAILED_ROWS_PREVIEW_LIMIT


# ========== on_conflict 处理（spec §2.21） ==========


class TestOnConflict:
    """on_conflict=skip / overwrite / fail_fast 不同行为。"""

    async def test_execute_skip_skips_existing_records(self, db_session, file_storage):
        """spec §2.21：on_conflict=skip → 已存在记录跳过，skipped_count += 1。"""
        dept = _make_dept(8401, "QA-Exec-Dept-SK")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9601, "QA_EXEC_SK", [super_role], [dept])
        existing_user = User(
            user_id=9610,
            user_name="QA_SK_EXISTING",
            hashed_password="x",
            status="1",
        )
        db_session.add_all([dept, operator, existing_user])
        await db_session.flush()

        records = [
            _make_record(2, "QA_SK_EXISTING", dept_input="QA-Exec-Dept-SK"),
            _make_record(3, "QA_SK_NEW", dept_input="QA-Exec-Dept-SK"),
        ]
        batch = await _setup_preview(db_session, records, operator, on_conflict="skip")

        result = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
            on_conflict="skip",
        )

        assert result.success_count == 1
        assert result.skipped_count == 1
        assert result.failed_count == 0

    async def test_execute_fail_fast_records_existing_as_failed(
        self, db_session, file_storage
    ):
        """spec §2.21：on_conflict=fail_fast → 已存在记录进 failed_rows。"""
        dept = _make_dept(8402, "QA-Exec-Dept-FF")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9602, "QA_EXEC_FF_OP", [super_role], [dept])
        existing_user = User(
            user_id=9620,
            user_name="QA_FF_EXISTING",
            hashed_password="x",
            status="1",
        )
        db_session.add_all([dept, operator, existing_user])
        await db_session.flush()

        records = [
            _make_record(2, "QA_FF_EXISTING", dept_input="QA-Exec-Dept-FF"),
            _make_record(3, "QA_FF_NEW", dept_input="QA-Exec-Dept-FF"),
        ]
        batch = await _setup_preview(
            db_session, records, operator, on_conflict="fail_fast"
        )

        result = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
            on_conflict="fail_fast",
        )

        assert result.success_count == 1
        assert result.failed_count == 1
        assert (
            result.failed_rows_preview[0].error_code == "AI_IMPORT_USERNAME_DUPLICATE"
        )


# ========== Batch Log（spec §2.28） ==========


class TestBatchLog:
    """execute 写 EXECUTE_START / CHUNK_PROGRESS / EXECUTE_FINISH log。"""

    async def test_execute_writes_lifecycle_logs(self, db_session, file_storage):
        """spec §2.28：每次状态转换 + chunk 完成 → 写 batch_log 行。"""
        dept = _make_dept(8501, "QA-Exec-Dept-LG")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9701, "QA_EXEC_LG", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_LG_U1", dept_input="QA-Exec-Dept-LG"),
            _make_record(3, "QA_LG_U2", dept_input="QA-Exec-Dept-LG"),
        ]
        batch = await _setup_preview(db_session, records, operator)

        await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        logs = (
            (
                await db_session.execute(
                    _select(UserImportBatchLog)
                    .where(UserImportBatchLog.batch_id == batch.batch_id)
                    .order_by(UserImportBatchLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        events = [log.event for log in logs]
        assert "EXECUTE_START" in events
        assert "CHUNK_PROGRESS" in events
        assert "EXECUTE_FINISH" in events


# ========== Status Transition（spec §2.26） ==========


class TestStatusTransition:
    """execute 完成后状态转 SUCCESS / PARTIAL_SUCCESS / FAILED。"""

    async def test_execute_transitions_to_success_when_all_succeed(
        self, db_session, file_storage
    ):
        dept = _make_dept(8601, "QA-Exec-Dept-SS")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9801, "QA_EXEC_SS", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_SS_U1", dept_input="QA-Exec-Dept-SS"),
            _make_record(3, "QA_SS_U2", dept_input="QA-Exec-Dept-SS"),
        ]
        batch = await _setup_preview(db_session, records, operator)

        await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        db_batch = (
            await db_session.execute(
                _select(UserImportBatch).where(
                    UserImportBatch.batch_id == batch.batch_id
                )
            )
        ).scalar_one()
        assert db_batch.status == ImportBatchStatus.SUCCESS
        assert db_batch.success_count == 2
        assert db_batch.finished_at is not None

    async def test_execute_transitions_to_partial_success_when_some_fail(
        self, db_session, file_storage
    ):
        dept = _make_dept(8602, "QA-Exec-Dept-PS")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9802, "QA_EXEC_PS", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_PS_OK", dept_input="QA-Exec-Dept-PS"),
            _make_record(3, "QA_PS_DUP", dept_input="QA-Exec-Dept-PS"),
            _make_record(4, "QA_PS_DUP", dept_input="QA-Exec-Dept-PS"),  # 冲突
        ]
        batch = await _setup_preview(db_session, records, operator)

        await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=operator,
            file_storage=file_storage,
        )

        db_batch = (
            await db_session.execute(
                _select(UserImportBatch).where(
                    UserImportBatch.batch_id == batch.batch_id
                )
            )
        ).scalar_one()
        assert db_batch.status == ImportBatchStatus.PARTIAL_SUCCESS
        assert db_batch.success_count == 2
        assert db_batch.failed_count == 1


# ========== 超管豁免（spec line 2662） ==========


class TestSuperAdminBypass:
    """超管 execute 阶段也豁免 Permission Boundary / Data Scope。"""

    async def test_execute_super_admin_creates_user_with_any_role(
        self, db_session, file_storage
    ):
        """spec line 2662：超管可分配任意角色 + 任意部门（execute 落库不被拦）。"""
        dept = _make_dept(8701, "QA-Exec-Dept-SA")
        any_role = _make_role(8702, "QA_R_ANY_EXEC", "QA-任意角色-Exec")
        admin_user = (
            await db_session.execute(
                _select(User).where(User.user_name == ADMIN_USERNAME)
            )
        ).scalar_one()
        db_session.add_all([dept, any_role])
        await db_session.flush()

        records = [
            _make_record(
                2,
                "QA_SA_EXEC_U1",
                dept_input="QA-Exec-Dept-SA",
                role_input="QA_R_ANY_EXEC",
            ),
        ]
        batch = await _setup_preview(db_session, records, admin_user)

        result = await batch_create_users_from_records(
            db_session,
            records,
            preview_token=batch.preview_token,
            file_bytes=_FILE_BYTES,
            filename="test.xlsx",
            reason="QA execute test",
            current_user=admin_user,
            file_storage=file_storage,
        )

        assert result.success_count == 1
        assert result.failed_count == 0
        # 用户绑定到 any_role
        created = (
            await db_session.execute(
                _select(User).where(User.user_name == "QA_SA_EXEC_U1")
            )
        ).scalar_one()
        assert any(r.role_code == "QA_R_ANY_EXEC" for r in created.roles)
