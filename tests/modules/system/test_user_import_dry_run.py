"""``dry_run_import_users`` 行为测试。

覆盖：
- 四象限分类：new / exists / conflict / out_of_scope
- preview_token 生成和 Redis 缓存
- file_sha256 与 records_hash 持久化
- CREATED → PREVIEW_DONE 状态迁移
- records 明细截断
- reason 校验

依赖 db_session outer-transaction fixture（不落库）；Redis 用 fakeredis 隔离。
"""

import hashlib
import json

import pytest
from fakeredis import aioredis as fakeredis_async
from sqlalchemy import select as _select

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_ALL,
    DATA_SCOPE_DEPT,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core import redis as redis_module
from app.core.exceptions import BusinessRuleException
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.user.constants import (
    MAX_PREVIEW_RECORDS,
    ImportBatchStatus,
)
from app.modules.system.user.import_service import dry_run_import_users
from app.modules.system.user.models import UserImportBatch
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
    dept_input: str = "QA-DryRun-Dept",
    role_input: str | None = None,
    employee_no: str | None = None,
) -> UserImportRecord:
    return UserImportRecord(
        row_num=row_num,
        user_name=user_name,
        employee_no=employee_no,
        dept_input=dept_input,
        role_input=role_input,
    )


def _make_records(n: int, prefix: str = "QA-DR") -> list[UserImportRecord]:
    return [
        _make_record(row_num=i, user_name=f"{prefix}_{i:04d}")
        for i in range(2, n + 2)  # row_num 从 2 起（row 1 是表头）
    ]


@pytest.fixture(autouse=True)
async def fake_redis(db_session, monkeypatch):  # noqa: ARG001 -- db_session 仅用于触发 fixture 顺序
    """autouse：在 db_session reset 之后替换 redis_client 为 fakeredis。

    依赖 db_session：conftest 的 db_session fixture 内部会 _reset_redis_client()
    把 redis_module.redis_client 指向真实 Redis；本 fixture 必须在它之后跑，
    才能 monkeypatch 把 redis_module.redis_client 覆盖回 fakeredis。

    dry_run_import_users 内部必调 setex；不替换会污染真实 Redis。
    需要直接断言 redis 状态的测试可拿返回值，不需要的忽略。
    """
    redis = fakeredis_async.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "redis_client", redis)
    try:
        yield redis
    finally:
        await redis.flushall()
        await redis.aclose()


def _file_bytes() -> bytes:
    """稳定的 file 内容（dry_run 不解析，只算 sha256，内容任意）。"""
    return b"dry-run-test-file-content"


# ========== 核心分类（spec line 2648-2651） ==========


class TestDryRunClassification:
    """四象限分类：new / exists / conflict / out_of_scope。"""

    async def test_dry_run_classifies_all_new(self, db_session):
        """spec 用例 1：全新 records → new_count = N，exists/conflict/out_of_scope 全 0。"""
        dept = _make_dept(7101, "QA-DryRun-Dept")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9201, "QA_DR_Admin", [super_role], [dept])
        records = _make_records(3, prefix="QA-DR-New")
        db_session.add_all([dept, operator])
        await db_session.flush()

        result, batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="test.xlsx",
            reason="QA dry_run 全新",
        )

        assert result.new_count == 3
        assert result.exists_count == 0
        assert result.conflict_count == 0
        assert result.out_of_scope_count == 0
        assert result.total == 3
        assert batch.summary_new == 3
        assert batch.status == ImportBatchStatus.PREVIEW_DONE

    async def test_dry_run_classifies_exists_by_username(self, db_session):
        """spec 用例 2：record.user_name 已存在 → exists_count = N。

        resolve_existing_user 兜底按 user_name 命中 → matched_by_employee_no=False
        → 归入 exists_records。
        """
        dept = _make_dept(7201, "QA-DryRun-Dept-E")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9202, "QA_DR_Admin_E", [super_role], [dept])
        existing_user = User(
            user_id=9210,
            user_name="QA_Exists_User",
            hashed_password="x",
            status="1",
        )
        db_session.add_all([dept, operator, existing_user])
        await db_session.flush()

        records = [
            _make_record(2, "QA_Exists_User", dept_input="QA-DryRun-Dept-E"),  # 已存在
            _make_record(3, "QA_New_Alone", dept_input="QA-DryRun-Dept-E"),  # 新建
        ]
        result, _batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="test.xlsx",
            reason="QA dry_run 含已存在",
        )

        assert result.exists_count == 1
        assert result.new_count == 1
        assert result.exists_records[0].user_name == "QA_Exists_User"

    async def test_dry_run_classifies_conflict_dept_not_found(self, db_session):
        """spec 用例 3a：dept_input 反查失败 → conflict（spec line 2061）。"""
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9203, "QA_DR_Admin_C", [super_role])
        db_session.add(operator)
        await db_session.flush()

        records = [
            _make_record(2, "QA_Conflict_Dept", dept_input="QA-NotExist-Dept"),
        ]
        result, _batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="test.xlsx",
            reason="QA dry_run dept 冲突",
        )

        assert result.conflict_count == 1
        assert result.new_count == 0
        assert result.conflict_records[0].error_code == "AI_IMPORT_DEPT_NOT_FOUND"
        assert result.conflict_records[0].field == "dept_input"

    async def test_dry_run_classifies_conflict_role_not_found(self, db_session):
        """spec 用例 3b：role_input 反查失败 → conflict。"""
        dept = _make_dept(7301, "QA-DryRun-Dept-R")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9204, "QA_DR_Admin_R", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = [
            _make_record(
                2,
                "QA_Conflict_Role",
                dept_input="QA-DryRun-Dept-R",
                role_input="QA_R_NOT_EXIST",
            ),
        ]
        result, _batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="test.xlsx",
            reason="QA dry_run role 冲突",
        )

        assert result.conflict_count == 1
        assert result.conflict_records[0].error_code == "AI_IMPORT_ROLE_NOT_FOUND"
        assert result.conflict_records[0].field == "role_input"

    async def test_dry_run_classifies_out_of_scope_role(self, db_session):
        """spec 用例 4a：HR 给导入用户分配自己不拥有的角色 → out_of_scope。

        Permission Boundary 校验在 dry_run 阶段就识别（spec line 2667）。
        """
        dept = _make_dept(7401, "QA-DryRun-Dept-OOS")
        hr_role = _make_role(
            7402, "QA_R_HR_OOS", "QA-HR-OOS", data_scope=DATA_SCOPE_ALL
        )
        forbidden_role = _make_role(
            7403, "QA_R_FORBIDDEN_OOS", "QA-Forbidden-OOS", data_scope=DATA_SCOPE_ALL
        )
        operator = _make_user(9205, "QA_DR_HR", [hr_role], [dept])
        db_session.add_all([dept, hr_role, forbidden_role, operator])
        await db_session.flush()

        records = [
            _make_record(
                2,
                "QA_OOS_New",
                dept_input="QA-DryRun-Dept-OOS",
                role_input="QA_R_FORBIDDEN_OOS",
            ),
        ]
        result, _batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="test.xlsx",
            reason="QA dry_run role 越界",
        )

        assert result.out_of_scope_count == 1
        assert result.new_count == 0
        assert (
            result.out_of_scope_records[0].error_code == "AI_IMPORT_ROLE_OUT_OF_SCOPE"
        )

    async def test_dry_run_classifies_out_of_scope_dept(self, db_session):
        """spec 用例 4b：dept 越界（DATA_SCOPE_DEPT 限定本部门）→ out_of_scope。"""
        own_dept = _make_dept(7501, "QA-DryRun-Own")
        other_dept = _make_dept(7502, "QA-DryRun-Other")
        hr_role = _make_role(
            7503, "QA_R_HR_DEPT", "QA-HR-DEPT", data_scope=DATA_SCOPE_DEPT
        )
        operator = _make_user(9206, "QA_DR_DEPT_MGR", [hr_role], [own_dept])
        db_session.add_all([own_dept, other_dept, hr_role, operator])
        await db_session.flush()

        records = [
            _make_record(2, "QA_OOS_Dept", dept_input="QA-DryRun-Other"),
        ]
        result, _batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="test.xlsx",
            reason="QA dry_run dept 越界",
        )

        assert result.out_of_scope_count == 1
        assert (
            result.out_of_scope_records[0].error_code == "AI_IMPORT_DEPT_OUT_OF_SCOPE"
        )


# ========== Preview Token 与 Redis 缓存 ==========


class TestPreviewToken:
    """preview_token 生成 + Redis cache + 三重校验字段写入。"""

    async def test_dry_run_creates_batch_row_with_preview_token(self, db_session):
        """dry_run 写入 sys_user_import_batch 行，含唯一 preview_token。"""
        dept = _make_dept(7601, "QA-DryRun-Dept-PV")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9207, "QA_DR_PV", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = _make_records(1, prefix="QA-DR-PV")
        _result, batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="preview-test.xlsx",
            reason="QA preview token",
        )

        # DB 行可查
        db_row = (
            await db_session.execute(
                _select(UserImportBatch).where(
                    UserImportBatch.batch_id == batch.batch_id
                )
            )
        ).scalar_one()
        assert db_row.preview_token == batch.preview_token
        assert db_row.preview_token  # 非空
        assert db_row.operator_id == operator.user_id
        assert db_row.filename == "preview-test.xlsx"

    async def test_dry_run_writes_redis_cache_token_to_batch_id(
        self, db_session, fake_redis
    ):
        """Redis 仅缓存 preview_token → batch_id，TTL 为 10 分钟。"""
        dept = _make_dept(7602, "QA-DryRun-Dept-RC")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9208, "QA_DR_RC", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        _result, batch = await dry_run_import_users(
            db_session,
            _make_records(1, prefix="QA-DR-RC"),
            operator,
            file_bytes=_file_bytes(),
            filename="redis-cache.xlsx",
            reason="QA redis cache",
        )

        cached = await fake_redis.get(f"user_import:preview:{batch.preview_token}")
        assert cached is not None
        payload = json.loads(cached)
        assert payload["batch_id"] == batch.batch_id
        # spec line 2696：Redis value 不含 records
        assert "records" not in payload
        assert "file_bytes" not in payload

    async def test_dry_run_redis_cache_ttl_is_600_seconds(self, db_session, fake_redis):
        """spec line 557：setex TTL=600（10min）。"""
        dept = _make_dept(7603, "QA-DryRun-Dept-TTL")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9209, "QA_DR_TTL", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        _result, batch = await dry_run_import_users(
            db_session,
            _make_records(1, prefix="QA-DR-TTL"),
            operator,
            file_bytes=_file_bytes(),
            filename="ttl.xlsx",
            reason="QA TTL",
        )

        ttl = await fake_redis.ttl(f"user_import:preview:{batch.preview_token}")
        # 允许 ±5s 漂移（fakeredis ttl 计时）
        assert 595 <= ttl <= 600

    async def test_dry_run_computes_file_sha256(self, db_session):
        """file_sha256 等于 sha256(file_bytes)，供执行前校验。"""
        dept = _make_dept(7604, "QA-DryRun-Dept-SHA")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9210, "QA_DR_SHA", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        file_bytes = b"sha256-test-content"
        _result, batch = await dry_run_import_users(
            db_session,
            _make_records(1, prefix="QA-DR-SHA"),
            operator,
            file_bytes=file_bytes,
            filename="sha.xlsx",
            reason="QA sha256",
        )

        expected = hashlib.sha256(file_bytes).hexdigest()
        assert batch.file_sha256 == expected

    async def test_dry_run_computes_records_hash(self, db_session):
        """records_hash 非空，防止预检后记录内容被修改。"""
        dept = _make_dept(7605, "QA-DryRun-Dept-RH")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9211, "QA_DR_RH", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = _make_records(2, prefix="QA-DR-RH")
        _result, batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="rh.xlsx",
            reason="QA records hash",
        )

        assert batch.records_hash
        assert len(batch.records_hash) == 64  # sha256 hex


# ========== 状态机 ==========


class TestStateMachine:
    """dry_run 内部 CREATED → PREVIEW_DONE 流转。"""

    async def test_dry_run_transitions_created_to_preview_done(self, db_session):
        """spec line 1113：CREATED → PREVIEW_DONE 在 dry_run 函数末尾。"""
        dept = _make_dept(7701, "QA-DryRun-Dept-SM")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9212, "QA_DR_SM", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        _result, batch = await dry_run_import_users(
            db_session,
            _make_records(1, prefix="QA-DR-SM"),
            operator,
            file_bytes=_file_bytes(),
            filename="sm.xlsx",
            reason="QA state machine",
        )

        assert batch.status == ImportBatchStatus.PREVIEW_DONE


# ========== Records Truncation ==========


class TestRecordsTruncation:
    """spec line 1679 + 2839：超出 MAX_PREVIEW_RECORDS → 截断 + truncated 标志。"""

    async def test_dry_run_truncates_conflict_records_over_limit(self, db_session):
        """MAX+1 个 conflict → conflict_records 截断到 MAX + truncated=True。"""
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9213, "QA_DR_TRUNC", [super_role])
        db_session.add(operator)
        await db_session.flush()

        # 全部 dept 反查失败 → 全 conflict
        records = [
            _make_record(
                row_num=i,
                user_name=f"QA_Trunc_{i:05d}",
                dept_input=f"QA-NonExist-{i:05d}",
            )
            for i in range(2, MAX_PREVIEW_RECORDS + 3)  # MAX+1 行
        ]
        result, batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="trunc.xlsx",
            reason="QA truncation over",
        )

        assert len(result.conflict_records) == MAX_PREVIEW_RECORDS
        assert result.conflict_records_truncated is True
        # summary 仍写原始计数（不截断）
        assert batch.summary_conflict == MAX_PREVIEW_RECORDS + 1

    async def test_dry_run_no_truncation_when_under_limit(self, db_session):
        """spec 用例：conflict_count < MAX → conflict_records_truncated=False。"""
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9214, "QA_DR_NOTRUNC", [super_role])
        db_session.add(operator)
        await db_session.flush()

        records = [
            _make_record(2, "QA_NoTrunc_1", dept_input="QA-NonExist-A"),
            _make_record(3, "QA_NoTrunc_2", dept_input="QA-NonExist-B"),
        ]
        result, _batch = await dry_run_import_users(
            db_session,
            records,
            operator,
            file_bytes=_file_bytes(),
            filename="notrunc.xlsx",
            reason="QA no truncation",
        )

        assert result.conflict_count == 2
        assert result.conflict_records_truncated is False


# ========== Reason 校验 ==========


class TestReasonValidation:
    """dry-run 入口兜底校验 reason 必填且长度为 1-256 字符。"""

    async def test_dry_run_reason_required(self, db_session):
        """reason 缺失 / 全空白 → AI_IMPORT_REASON_REQUIRED。"""
        dept = _make_dept(7801, "QA-DryRun-Dept-RSN")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9215, "QA_DR_RSN", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = _make_records(1, prefix="QA-DR-RSN")
        with pytest.raises(BusinessRuleException) as exc:
            await dry_run_import_users(
                db_session,
                records,
                operator,
                file_bytes=_file_bytes(),
                filename="reason.xlsx",
                reason="   ",  # 全空白
            )
        assert exc.value.error_code == "AI_IMPORT_REASON_REQUIRED"

    async def test_dry_run_reason_too_long_rejected(self, db_session):
        """reason 长度上限为 256 字符。"""
        dept = _make_dept(7802, "QA-DryRun-Dept-RSL")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9216, "QA_DR_RSL", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        records = _make_records(1, prefix="QA-DR-RSL")
        with pytest.raises(BusinessRuleException) as exc:
            await dry_run_import_users(
                db_session,
                records,
                operator,
                file_bytes=_file_bytes(),
                filename="reason-long.xlsx",
                reason="x" * 257,
            )
        assert exc.value.error_code == "AI_IMPORT_REASON_REQUIRED"

    async def test_dry_run_persists_reason_in_batch(self, db_session):
        """reason 写入 batch.reason 并进入审计链路。"""
        dept = _make_dept(7803, "QA-DryRun-Dept-RSP")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9217, "QA_DR_RSP", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        _result, batch = await dry_run_import_users(
            db_session,
            _make_records(1, prefix="QA-DR-RSP"),
            operator,
            file_bytes=_file_bytes(),
            filename="reason-persist.xlsx",
            reason="2026年8月 HR 入职名单同步",
        )

        assert batch.reason == "2026年8月 HR 入职名单同步"


# ========== 超管豁免（spec line 2662） ==========


class TestSuperAdminBypass:
    """超管导入可分配任意角色 + 任意部门（dry_run 阶段豁免）。"""

    async def test_dry_run_super_admin_no_out_of_scope(self, db_session):
        """超管导入分配任意角色 → out_of_scope_count=0（line 2662）。"""
        dept = _make_dept(7901, "QA-DryRun-Dept-SA")
        # 用 init_db.py seed 的 admin user（避免 user_name UniqueViolation）
        admin_user = (
            await db_session.execute(
                _select(User).where(User.user_name == ADMIN_USERNAME)
            )
        ).scalar_one()
        any_role = _make_role(7902, "QA_R_ANY_DRY", "QA-任意角色-DryRun")
        db_session.add_all([dept, any_role])
        await db_session.flush()

        records = [
            _make_record(
                2,
                "QA_SA_New",
                dept_input="QA-DryRun-Dept-SA",
                role_input="QA_R_ANY_DRY",
            ),
        ]
        result, _batch = await dry_run_import_users(
            db_session,
            records,
            admin_user,
            file_bytes=_file_bytes(),
            filename="sa.xlsx",
            reason="QA super admin bypass",
        )

        assert result.out_of_scope_count == 0
        assert result.new_count == 1


# ========== CREATED → FAILED 集成路径 ==========


class TestCreatedToFailedTransition:
    """dry-run 阶段失败时批次从 CREATED 迁移为 FAILED。

    覆盖两个失败分支：
    - 0 行 records（解析后无有效数据）
    - 分类阶段意外异常（如 DB 错误）

    反例（旧行为）：batch 停留在 CREATED 成僵尸行，审计看到「创建一年后还是 CREATED」。
    """

    async def test_state_created_to_failed_on_zero_records(self, db_session):
        """0 行 records → CREATED → FAILED + 抛 AI_IMPORT_EMPTY_FILE。"""
        dept = _make_dept(7950, "QA-DryRun-Dept-Empty")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9250, "QA_DR_EMPTY", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await dry_run_import_users(
                db_session,
                [],  # 0 行
                operator,
                file_bytes=_file_bytes(),
                filename="empty.xlsx",
                reason="QA zero records",
            )
        assert exc.value.error_code == "AI_IMPORT_EMPTY_FILE"

        # batch 落 FAILED（用 reason 反查 + populate_existing 强制覆盖 identity map
        # 旧对象 —— _transition_batch_status 用 raw UPDATE 绕过 ORM synchronize）
        batch = (
            await db_session.execute(
                _select(UserImportBatch)
                .where(UserImportBatch.reason == "QA zero records")
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert batch.status == ImportBatchStatus.FAILED

    async def test_state_created_to_failed_on_classification_error(
        self, db_session, monkeypatch
    ):
        """分类阶段异常 → CREATED → FAILED + 原异常 re-raise。"""
        dept = _make_dept(7951, "QA-DryRun-Dept-CE")
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        operator = _make_user(9251, "QA_DR_CE", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        # mock _classify_records 抛 DB 错误
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("QA simulated DB error during classification")

        monkeypatch.setattr(
            "app.modules.system.user.import_service._classify_records", _boom
        )

        records = _make_records(1, prefix="QA-DR-CE")
        with pytest.raises(RuntimeError, match="QA simulated DB error"):
            await dry_run_import_users(
                db_session,
                records,
                operator,
                file_bytes=_file_bytes(),
                filename="classification_error.xlsx",
                reason="QA classification error",
            )

        # batch 落 FAILED
        batch = (
            await db_session.execute(
                _select(UserImportBatch)
                .where(UserImportBatch.reason == "QA classification error")
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert batch.status == ImportBatchStatus.FAILED
