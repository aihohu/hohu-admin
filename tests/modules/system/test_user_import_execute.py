"""``batch_create_users_from_records`` 行为测试。

覆盖：
- preview_token 的批次、文件和记录哈希校验
- CAS 将 PREVIEW_DONE 原子迁移为 RUNNING
- 分块和 savepoint 落库
- 将 user_name 唯一键冲突映射为 AI_IMPORT_USERNAME_DUPLICATE
- on_conflict 的 skip、overwrite 和 fail_fast 行为
- 批次执行与分块进度日志
- 失败行文件生成
- SUCCESS、PARTIAL_SUCCESS 和 FAILED 状态迁移

测试通过 dry_run_import_users 走完整 preview 流程建 PREVIEW_DONE batch，
再调 batch_create_users_from_records 验证 execute 阶段语义。
"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fakeredis_async
from sqlalchemy import delete as _delete
from sqlalchemy import select as _select
from sqlalchemy.orm import selectinload

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_ALL,
    SUPER_ADMIN_ROLE_CODE,
    USER_ROLE_CODE,
)
from app.core import redis as redis_module
from app.core.exceptions import BusinessRuleException
from app.core.file_storage import MockFileStorage
from app.core.id_generator import next_id
from app.core.security import verify_password
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.config import Config
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.user.constants import (
    FAILED_ROWS_PREVIEW_LIMIT,
    USER_IMPORT_CHUNK_SIZE,
    ImportBatchStatus,
)
from app.modules.system.user.import_service import (
    ImportAuthorizationResolution,
    _classify_records,
    _lock_import_authorization_targets,
    batch_create_users_from_records,
    dry_run_import_users,
)
from app.modules.system.user.models import UserImportBatch, UserImportBatchLog
from app.modules.system.user.schemas import ImportResult, UserImportRecord

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
    """为每个测试提供独立的 MockFileStorage。"""
    return MockFileStorage()


async def _seed_default_password(
    db_session,
    password: str = "QA-Default-Pwd-123",
) -> None:
    """设置 ``sys_config.auth:default_password``。

    Why DELETE-first: db_session is outer-transaction rollback, but the dev DB
    itself may already hold a seeded auth:default_password row (init_db.py or
    prior manual test). INSERT collides with the unique key regardless of
    transaction isolation; DELETE first to keep the helper idempotent.
    """
    await db_session.execute(
        _delete(Config).where(Config.config_key == "auth:default_password")
    )
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


# ========== 预检令牌三重校验 ==========


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


# ========== 幂等执行 ==========


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


# ========== 分块与 Savepoint ==========


class TestChunkSavepoint:
    """chunk 100 rows + 行级 savepoint + IntegrityError 区分。"""

    async def test_execute_creates_users_with_default_password(
        self, db_session, file_storage
    ):
        """新用户使用配置的默认密码哈希入库。"""
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
        """同一批次内重复 user_name 时，第二条进入 failed_rows。

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
        """失败行写入 Excel，路径保存到 batch.failed_rows_file。"""
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
        """failed_rows_preview 仅保留前 20 条供提示展示。"""
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


# ========== on_conflict 处理 ==========


class TestOnConflict:
    """on_conflict=skip / overwrite / fail_fast 不同行为。"""

    async def test_execute_skip_skips_existing_records(self, db_session, file_storage):
        """on_conflict=skip 时跳过已存在记录并增加 skipped_count。"""
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
        """on_conflict=fail_fast 时已存在记录进入 failed_rows。"""
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


# ========== 批次日志 ==========


class TestBatchLog:
    """execute 写 EXECUTE_START / CHUNK_PROGRESS / EXECUTE_FINISH log。"""

    async def test_execute_writes_lifecycle_logs(self, db_session, file_storage):
        """每次状态迁移和分块完成都写入 batch_log。"""
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


# ========== 状态迁移 ==========


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


async def test_execute_without_role_input_assigns_fixed_default_role(
    db_session, file_storage
):
    """A new imported user without an explicit role must receive fixed R_USER."""
    dept = _make_dept(8791, "QA-Exec-Dept-Default-Role")
    admin_user = (
        await db_session.execute(_select(User).where(User.user_name == ADMIN_USERNAME))
    ).scalar_one()
    db_session.add(dept)
    await db_session.flush()
    records = [
        _make_record(
            2,
            "QA_DEF_ROLE_U1",
            dept_input=dept.dept_name,
            role_input=None,
        )
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

    created = (
        await db_session.execute(
            _select(User)
            .where(User.user_name == "QA_DEF_ROLE_U1")
            .options(selectinload(User.roles))
        )
    ).scalar_one()
    assert result.success_count == 1
    assert [role.role_code for role in created.roles] == [USER_ROLE_CODE]


async def test_execute_rejects_the_whole_batch_when_one_role_is_unauthorized(
    db_session, file_storage
):
    dept = _make_dept(next_id(), f"QA-Atomic-Dept-{next_id()}")
    import_menu = Menu(
        menu_id=next_id(),
        menu_name=f"QA import {next_id()}",
        menu_type="F",
        permission="system:user:import",
        status="1",
    )
    role_auth_menu = Menu(
        menu_id=next_id(),
        menu_name=f"QA role auth {next_id()}",
        menu_type="F",
        permission=USER_ROLE_AUTH_PERMISSION,
        status="1",
    )
    dept_list_menu = Menu(
        menu_id=next_id(),
        menu_name=f"QA dept list {next_id()}",
        menu_type="F",
        permission="system:dept:list",
        status="1",
    )
    delegated_menu = Menu(
        menu_id=next_id(),
        menu_name=f"QA delegated {next_id()}",
        menu_type="F",
        permission=f"qa:delegated:{next_id()}:read",
        status="1",
    )
    outside_menu = Menu(
        menu_id=next_id(),
        menu_name=f"QA outside {next_id()}",
        menu_type="F",
        permission=f"qa:outside:{next_id()}:read",
        status="1",
    )
    actor_role = _make_role(
        next_id(),
        f"QA_R_ATOMIC_ACTOR_{next_id()}",
        "QA Atomic Actor",
    )
    actor_role.menus = [
        import_menu,
        role_auth_menu,
        dept_list_menu,
        delegated_menu,
    ]
    allowed_role = _make_role(
        next_id(),
        f"QA_R_ATOMIC_ALLOWED_{next_id()}",
        "QA Atomic Allowed",
    )
    allowed_role.menus = [delegated_menu]
    forbidden_role = _make_role(
        next_id(),
        f"QA_R_ATOMIC_FORBIDDEN_{next_id()}",
        "QA Atomic Forbidden",
    )
    forbidden_role.menus = [outside_menu]
    operator = _make_user(next_id(), f"QA_ATOMIC_ACTOR_{next_id()}", [actor_role])
    db_session.add_all(
        [
            dept,
            import_menu,
            role_auth_menu,
            dept_list_menu,
            delegated_menu,
            outside_menu,
            actor_role,
            allowed_role,
            forbidden_role,
            operator,
        ]
    )
    await db_session.flush()
    records = [
        _make_record(
            2,
            "QA_ATOMIC_OK",
            dept_input=dept.dept_name,
            role_input=allowed_role.role_code,
        ),
        _make_record(
            3,
            "QA_ATOMIC_NO",
            dept_input=dept.dept_name,
            role_input=forbidden_role.role_code,
        ),
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

    created = list(
        (
            await db_session.execute(
                _select(User).where(
                    User.user_name.in_(["QA_ATOMIC_OK", "QA_ATOMIC_NO"])
                )
            )
        ).scalars()
    )
    assert result.status == ImportBatchStatus.FAILED.value
    assert result.success_count == 0
    assert created == []


@pytest.mark.parametrize(
    "changes",
    [
        {"dept_id": 456, "dept_error": None},
        {"role_ids": (789,), "role_error": None},
        {
            "target_user_id": 999,
            "prospective_user_id": None,
            "target_role_ids": (789,),
        },
    ],
)
async def test_import_lock_rejects_a_reference_that_resolves_after_prelock(
    monkeypatch,
    changes,
):
    record = _make_record(2, "QA_PHANTOM", dept_input="QA-New-Dept")
    before = ImportAuthorizationResolution(
        row_num=2,
        dept_id=None,
        dept_error=("AI_IMPORT_DEPT_NOT_FOUND", "missing"),
        role_ids=None,
        role_error=("USER_DEFAULT_ROLE_NOT_AVAILABLE", "missing"),
        target_user_id=None,
        matched_by_employee_no=False,
        target_role_ids=(),
        target_dept_ids=(),
        target_status="1",
        prospective_user_id=123,
    )
    after = replace(before, **changes)
    resolver = AsyncMock(side_effect=[[before], [after]])
    lock_targets = AsyncMock(return_value=SimpleNamespace(user_id=42))
    ensure_permissions = AsyncMock()
    monkeypatch.setattr(
        "app.modules.system.user.import_service._resolve_import_authorization_targets",
        resolver,
    )
    monkeypatch.setattr(
        "app.modules.system.user.import_service.user_role_assignment_service."
        "lock_import_targets",
        lock_targets,
    )
    monkeypatch.setattr(
        "app.modules.system.user.import_service.user_role_assignment_service."
        "ensure_import_permissions",
        ensure_permissions,
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await _lock_import_authorization_targets(
            AsyncMock(),
            [record],
            SimpleNamespace(user_id=42),
            has_role_column=False,
        )

    assert exc_info.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    ensure_permissions.assert_not_awaited()


async def test_import_classification_rejects_duplicate_existing_targets(
    monkeypatch,
):
    records = [
        _make_record(2, "QA_DUPLICATE_A", role_input="R_SAFE"),
        _make_record(3, "QA_DUPLICATE_B"),
    ]
    resolutions = [
        ImportAuthorizationResolution(
            row_num=record.row_num,
            dept_id=456,
            dept_error=None,
            role_ids=((789,) if record.role_input else None),
            role_error=None,
            target_user_id=999,
            matched_by_employee_no=True,
            target_role_ids=(321,),
            target_dept_ids=(123,),
            target_status="1",
            prospective_user_id=None,
        )
        for record in records
    ]
    monkeypatch.setattr(
        "app.modules.system.user.import_service.grant_authority_service.build",
        AsyncMock(return_value=SimpleNamespace(accessible_dept_ids=None)),
    )
    validate_departments = AsyncMock()
    validate_roles = AsyncMock()
    monkeypatch.setattr(
        "app.modules.system.user.import_service."
        "user_role_assignment_service.validate_import_role_assignment",
        validate_roles,
    )
    monkeypatch.setattr(
        "app.modules.system.user.import_service."
        "user_department_assignment_service.validate_import_department_assignment",
        validate_departments,
    )

    new, existing, conflicts, out_of_scope = await _classify_records(
        AsyncMock(),
        records,
        SimpleNamespace(user_id=42),
        has_role_column=True,
        resolutions=resolutions,
    )

    assert new == []
    assert existing == []
    assert out_of_scope == []
    assert [row.error_code for row in conflicts] == [
        "AI_IMPORT_DUPLICATE_TARGET",
        "AI_IMPORT_DUPLICATE_TARGET",
    ]
    validate_roles.assert_not_awaited()
    validate_departments.assert_not_awaited()


# ========== 并发执行 ==========


class TestConcurrentExecute:
    """并发执行同一批次时，CAS 保证仅一个调用成功。"""

    async def test_concurrent_execute_same_batch(self, db_session, file_storage):
        """asyncio.gather 模拟并发，CAS 保证仅一次成功且只创建一个用户。

        SQLAlchemy ``AsyncSession`` 不支持单 session 并发 IO（``MissingGreenlet``），
        真并发场景下 gather 会部分抛 ``MissingGreenlet``；本测试用 ``return_exceptions=True``
        容错，**关键不变量**是「无论多少 coroutine 抢占，最终入库用户数 == 1」（CAS 在 SQL
        层 ``UPDATE WHERE status='PREVIEW_DONE'`` 保持原子性）。

        SQL 层 CAS rowcount 互斥由 ``test_import_state.py::test_state_cas_prevents_race``
        覆盖；service 层 RUNNING 重放由 ``test_execute_same_token_twice_running_concurrent``
        覆盖；本测试补「gather 不破坏不变量」端到端验证。

        **反例**: 若 CAS 失效（如改成 ``SELECT status`` + Python 判断 + ``UPDATE``）→
        多个 coroutine 都看到 PREVIEW_DONE → 都进 chunk loop → 重复创建多个用户。
        **回归**: ``await _count_users_by_prefix(db_session, "QA_CE_U") == 1``。
        """
        dept = _make_dept(8901, "QA-Exec-Dept-CE")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9901, "QA_EXEC_CE", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_CE_U1", dept_input="QA-Exec-Dept-CE")]
        batch = await _setup_preview(db_session, records, operator)

        async def _execute():
            return await batch_create_users_from_records(
                db_session,
                records,
                preview_token=batch.preview_token,
                file_bytes=_FILE_BYTES,
                filename="test.xlsx",
                reason="QA execute test",
                current_user=operator,
                file_storage=file_storage,
            )

        # gather 3 并发：AsyncSession 单 session 限制下，部分 coroutine 可能撞
        # MissingGreenlet；return_exceptions=True 容错，重点验证「最终仅 1 用户入库」
        results = await asyncio.gather(
            _execute(),
            _execute(),
            _execute(),
            return_exceptions=True,
        )

        # 至少 1 个成功（idempotent_replay=False）
        successes = [
            r
            for r in results
            if isinstance(r, ImportResult) and not r.idempotent_replay
        ]
        assert len(successes) == 1

        # 核心不变量：CAS 防止重复入库
        assert await _count_users_by_prefix(db_session, "QA_CE_U") == 1

    async def test_execute_same_token_twice_running_concurrent(
        self, db_session, file_storage
    ):
        """CAS 失败且状态为 RUNNING 时返回 AI_IMPORT_BATCH_RUNNING。

        模拟并发场景：另一 coroutine 已把 status 从 PREVIEW_DONE 转 RUNNING（CAS 已抢走），
        本调用 CAS 失败后 ``_handle_idempotent_replay`` 读到 RUNNING → 抛 ``AI_IMPORT_BATCH_RUNNING``。

        **反例**: 不区分 RUNNING vs FAILED/EXPIRED 都抛 ``AI_IMPORT_ALREADY_EXECUTED``
        → 前端无法提示「请等待」vs「已结束不能重放」，UX 退化。
        **回归**: 本测试严格断言 ``error_code == "AI_IMPORT_BATCH_RUNNING"``。
        """
        dept = _make_dept(8902, "QA-Exec-Dept-RUN")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9902, "QA_EXEC_RUN", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_RUN_U1", dept_input="QA-Exec-Dept-RUN")]
        batch = await _setup_preview(db_session, records, operator)

        # 手动模拟另一 coroutine 已 CAS 转 RUNNING（chunk loop 进行中）
        batch.status = ImportBatchStatus.RUNNING
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
        assert exc.value.error_code == "AI_IMPORT_BATCH_RUNNING"

    async def test_execute_expired_batch_rejected(self, db_session, file_storage):
        """CAS 失败且状态为 EXPIRED 时返回 AI_IMPORT_ALREADY_EXECUTED。

        EXPIRED 是终态（preview TTL 10min 过期由 cleanup cron 转换），重放应被拒绝。
        区别于 RUNNING（可重试）：EXPIRED 不可恢复，必须重新走 dry_run。

        **反例**: 允许 EXPIRED 重放 → 用户拿到 10min 前的 preview_token 直接 execute
        → 跳过重新 dry_run → 但 Redis cache 已被 cleanup 清，会走 DB fallback →
        可能基于过时数据 execute。
        **回归**: 本测试严格断言 ``error_code == "AI_IMPORT_ALREADY_EXECUTED"``。
        """
        dept = _make_dept(8903, "QA-Exec-Dept-EXP")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9903, "QA_EXEC_EXP", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_EXP_U1", dept_input="QA-Exec-Dept-EXP")]
        batch = await _setup_preview(db_session, records, operator)

        batch.status = ImportBatchStatus.EXPIRED
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


# ========== Redis 缓存回退 ==========


class TestRedisCacheFallback:
    """Redis 缓存缺失或损坏时回退数据库反查。

    preview_token 是 SoT（Source of Truth），Redis 仅加速。即使 Redis 全丢或被篡改，
    execute 仍可凭 preview_token 从数据库找到批次。
    """

    async def test_preview_cache_missing_falls_back_to_db(
        self, db_session, file_storage, fake_redis
    ):
        """Redis 数据全部丢失时，数据库回退仍可完成执行。

        场景：Redis 故障 / flushall / key 过期后，execute 凭 preview_token 反查
        ``sys_user_import_batch.preview_token`` 唯一索引拿到 batch 行，三重校验通过 → 落库。

        **反例**: Redis 是 SoT（cache 丢失则 token 失效）→ Redis 一抖动所有 in-flight
        import 全部卡死，用户必须重新上传文件 + 重新 dry_run。
        **回归**: 本测试 ``await fake_redis.flushall()`` 后 execute 仍返回 ``success_count == 1``。
        """
        dept = _make_dept(8904, "QA-Exec-Dept-MISS")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9904, "QA_EXEC_MISS", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_MISS_U1", dept_input="QA-Exec-Dept-MISS")]
        batch = await _setup_preview(db_session, records, operator)

        # 清空 Redis（模拟 cache miss / 故障）
        await fake_redis.flushall()

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
        assert result.idempotent_replay is False
        assert await _count_users_by_prefix(db_session, "QA_MISS_U") == 1

    async def test_preview_cache_corrupted_falls_back_to_db(
        self, db_session, file_storage, fake_redis
    ):
        """Redis 指向不存在批次时，数据库回退仍可完成执行。

        场景：运维误操作 / Redis 复制 bug 导致 cache value 被改。``get_batch_by_preview_token``
        先读 Redis 拿到 batch_id，查询不到记录后继续回退，
        再用 preview_token 反查 DB 拿到真实 batch → 三重校验通过 → 落库。

        **反例**: Redis 命中就信任（不验证 batch_id 存在）→ 篡改后 execute 找不到 batch
        抛 AI_IMPORT_PREVIEW_INVALID，用户必须重新 dry_run。
        **回归**: ``get_batch_by_preview_token`` 的 fall-through 逻辑由本测试覆盖。
        """
        dept = _make_dept(8905, "QA-Exec-Dept-CORR")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9905, "QA_EXEC_CORR", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [_make_record(2, "QA_CORR_U1", dept_input="QA-Exec-Dept-CORR")]
        batch = await _setup_preview(db_session, records, operator)

        # 篡改 Redis value：JSON 仍合法但 batch_id 指向不存在的批次
        cache_key = f"user_import:preview:{batch.preview_token}"
        await fake_redis.setex(
            cache_key,
            600,
            json.dumps({"batch_id": "non-existent-batch-id-tampered"}),
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
        )

        assert result.success_count == 1
        assert result.idempotent_replay is False
        assert await _count_users_by_prefix(db_session, "QA_CORR_U") == 1


# ========== 分块进度与致命错误日志 ==========


class TestBatchLogAdvanced:
    """验证 chunk_progress 计数和 fatal_error 审计。

    覆盖批次日志中的两个边界：
    - 多 chunk 的 CHUNK_PROGRESS 行计数
    - chunk 致命错误 → EXECUTE_FINISH 写 aborted 详情
    """

    async def test_log_records_chunk_progress_per_chunk(self, db_session, file_storage):
        """N 行产生 ceil(N/100) 个 CHUNK_PROGRESS，且 chunk_index 递增。

        USER_IMPORT_CHUNK_SIZE=100（constants.py），200 行 → 2 个 chunk → 2 条 CHUNK_PROGRESS。
        每条 detail.chunk_index 严格递增（0/1），便于前端按 chunk 维度绘制进度条。

        **反例**: 不写 chunk_index 或全写 0 → 前端无法区分「chunk 0 完成」vs「chunk 1 完成」，
        进度条卡在 50% 不动直到全部完成。
        **回归**: 本测试断言 chunk_index 严格为 [0, 1]。
        """
        dept = _make_dept(8906, "QA-Exec-Dept-CP")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9906, "QA_EXEC_CP", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        # 200 行 = 2 chunks（USER_IMPORT_CHUNK_SIZE=100）
        records = [
            _make_record(i, f"QA_CP_U{i}", dept_input="QA-Exec-Dept-CP")
            for i in range(2, 202)
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
                    .order_by(UserImportBatchLog.created_at, UserImportBatchLog.log_id)
                )
            )
            .scalars()
            .all()
        )
        chunk_logs = [log for log in logs if log.event == "CHUNK_PROGRESS"]

        # 200 / 100 = 2 chunks
        assert USER_IMPORT_CHUNK_SIZE == 100
        assert len(chunk_logs) == 2
        # chunk_index 严格递增
        assert chunk_logs[0].detail["chunk_index"] == 0
        assert chunk_logs[1].detail["chunk_index"] == 1
        # total_chunks 字段
        assert chunk_logs[0].detail["total_chunks"] == 2
        assert chunk_logs[1].detail["total_chunks"] == 2

    async def test_log_records_fatal_error_in_execute_finish(
        self, db_session, file_storage, monkeypatch
    ):
        """分块发生致命错误时写入 EXECUTE_FINISH.aborted，并将全批标记失败。

        模拟 _process_create_row 抛 RuntimeError（非 BusinessException / IntegrityError），
        chunk savepoint 自动 ROLLBACK → outer except 捕获 → aborted_error 写入 EXECUTE_FINISH
        detail + 当前 chunk + 后续 chunk 所有行进 failed_rows（error_code=AI_IMPORT_BATCH_ABORTED）。

        当前实现把 aborted 信息合并到 EXECUTE_FINISH.detail.aborted，
        不额外拆分 EXECUTE_FAILED 事件，以保持现有日志消费语义和
        已满足「致命错误可审计」需求）。

        **反例**: 致命错误静默 → batch 状态显示 SUCCESS 但实际 0 用户入库（数据不一致）。
        **回归**: 本测试断言 EXECUTE_FINISH.detail 含 ``aborted`` 键 + result.failed_count > 0。
        """
        dept = _make_dept(8907, "QA-Exec-Dept-FE")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(9907, "QA_EXEC_FE", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_FE_U1", dept_input="QA-Exec-Dept-FE"),
            _make_record(3, "QA_FE_U2", dept_input="QA-Exec-Dept-FE"),
        ]
        batch = await _setup_preview(db_session, records, operator)

        # 模拟致命错误：monkeypatch _process_create_row 抛 RuntimeError
        async def _raise_fatal(
            db,  # noqa: ARG001
            record,  # noqa: ARG001
            hashed_password,  # noqa: ARG001
            current_user,  # noqa: ARG001
            resolution,  # noqa: ARG001
            **_kwargs,
        ):
            raise RuntimeError("simulated fatal DB error")

        monkeypatch.setattr(
            "app.modules.system.user.import_service._process_create_row",
            _raise_fatal,
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
        )

        # 全部记录失败（chunk savepoint rollback + 后续 chunk 也归 failed）
        assert result.success_count == 0
        assert result.failed_count >= 1
        assert result.status == ImportBatchStatus.FAILED.value

        # EXECUTE_FINISH log 写入 aborted 详情（str(exc) 不含类型名，只含 message）
        logs = (
            (
                await db_session.execute(
                    _select(UserImportBatchLog)
                    .where(UserImportBatchLog.batch_id == batch.batch_id)
                    .order_by(UserImportBatchLog.created_at, UserImportBatchLog.log_id)
                )
            )
            .scalars()
            .all()
        )
        finish_logs = [log for log in logs if log.event == "EXECUTE_FINISH"]
        assert len(finish_logs) == 1
        assert "aborted" in finish_logs[0].detail
        # str(RuntimeError("...")) 只返回 message；类型名在 failed_rows.reason 里
        assert "simulated fatal DB error" in finish_logs[0].detail["aborted"]

        # failed_rows.reason 含 type(e).__name__（service 写 failed_rows 时拼了类型名）
        aborted_failed_rows = [
            fr for fr in result.failed_rows_preview if "RuntimeError" in fr.reason
        ]
        assert len(aborted_failed_rows) >= 1
