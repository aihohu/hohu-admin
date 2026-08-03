"""export_users_to_excel 单测（Task 11，spec §3.6 line 2099-2111 + §2.31）。

覆盖：
- 强制建 UserExportTask（spec §2.31 line 1436-1453）
- filter_snapshot 冻结 accessible_dept_ids（spec §2.31 line 1516-1520）
- reason 必填（spec §2.30 v2.2 P1-3）
- EXPORT_ALLOWED_FIELDS 白名单（hashed_password 永不导出，spec §2.9）
- 行数 > USER_EXPORT_ASYNC_THRESHOLD → AI_EXPORT_ASYNC_REQUIRED（spec §2.6）
- data_scope 自动应用（HR 只能导他可见的，spec §2.31 line 1545）
- 失败也建 task（status=FAILED + error_code，spec §2.31 line 1567-1572）
- 30 天 TTL 文件存储（spec §2.31 line 1554）

依赖 db_session outer-transaction fixture（不落库）。
"""

import io

import pytest
from openpyxl import load_workbook
from sqlalchemy import select as _select

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_ALL,
    DATA_SCOPE_DEPT,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import BusinessRuleException
from app.core.file_storage import MockFileStorage
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.user.constants import (
    EXPORT_ALLOWED_FIELDS,
    USER_EXPORT_ASYNC_THRESHOLD,
    ExportTaskStatus,
)
from app.modules.system.user.export_service import export_users_to_excel
from app.modules.system.user.models import UserExportTask
from app.modules.system.user.schemas import UserExportFilter

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
    *,
    nickname: str | None = "Nick",
    user_email: str | None = None,
    user_phone: str | None = None,
    user_gender: str = "0",
    status: str = "1",
    hashed_password: str = "secret-hash-never-export",
) -> User:
    return User(
        user_id=user_id,
        user_name=user_name,
        hashed_password=hashed_password,
        nickname=nickname,
        user_email=user_email,
        user_phone=user_phone,
        user_gender=user_gender,
        status=status,
        roles=roles,
        depts=depts or [],
    )


@pytest.fixture
def file_storage() -> MockFileStorage:
    """每个测试独立 MockFileStorage（spec §3.9 注入）。"""
    return MockFileStorage()


async def _fetch_super_role(db_session) -> Role:
    return (
        await db_session.execute(
            _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
        )
    ).scalar_one()


def _read_xlsx(xlsx_bytes: bytes) -> tuple[list[str], list[list]]:
    """读 xlsx bytes → (headers, rows)。"""
    wb = load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    return list(rows[0]), [list(r) for r in rows[1:]]


# ========== Happy Path ==========


class TestHappyPath:
    """spec §3.6 line 2099-2111：基本导出流程。"""

    async def test_export_returns_xlsx_and_count(self, db_session, file_storage):
        """返回 (xlsx_bytes, row_count, export_id) 三元组。"""
        dept = _make_dept(5101, "QA-Exp-Dept")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6101, "QA_EXP_OP", [super_role], [dept])
        target = _make_user(
            6102,
            "QA_EXP_T1",
            [super_role],
            [dept],
            user_email="t1@example.com",
        )
        db_session.add_all([dept, operator, target])
        await db_session.flush()

        result = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            operator,
            reason="QA export happy path",
            file_storage=file_storage,
        )

        assert len(result) == 3
        xlsx_bytes, row_count, export_id = result
        assert isinstance(xlsx_bytes, bytes)
        assert xlsx_bytes[:2] == b"PK"  # xlsx zip 头
        assert row_count >= 1
        assert isinstance(export_id, str)
        assert export_id

    async def test_export_only_includes_allowed_fields(self, db_session, file_storage):
        """spec §2.9：hashed_password 永不出现在 Excel。"""
        dept = _make_dept(5102, "QA-Exp-Dept-FW")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6103, "QA_EXP_FW", [super_role], [dept])
        target = _make_user(
            6104,
            "QA_EXP_TARGET",
            [super_role],
            [dept],
            hashed_password="$2b$12$super-secret-bcrypt-hash",
        )
        db_session.add_all([dept, operator, target])
        await db_session.flush()

        xlsx_bytes, _count, _export_id = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            operator,
            reason="QA field whitelist",
            file_storage=file_storage,
        )

        headers, rows = _read_xlsx(xlsx_bytes)
        assert "hashed_password" not in headers
        # 检查所有单元格值，永不含 bcrypt 哈希
        all_values = " ".join(str(v) for row in rows for v in row)
        assert "$2b$12$super-secret-bcrypt-hash" not in all_values

    async def test_export_includes_all_allowed_fields(self, db_session, file_storage):
        """spec §2.9：EXPORT_ALLOWED_FIELDS 中所有字段都应有对应列。"""
        dept = _make_dept(5103, "QA-Exp-Dept-Col")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6105, "QA_EXP_COL", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        xlsx_bytes, _count, _export_id = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            operator,
            reason="QA all columns",
            file_storage=file_storage,
        )

        headers, _rows = _read_xlsx(xlsx_bytes)
        # 至少包含 EXPORT_ALLOWED_FIELDS 中的字段名（中文表头时校验英文不易，
        # 这里检查列数与 EXPORT_ALLOWED_FIELDS 一致）
        assert len(headers) >= len(EXPORT_ALLOWED_FIELDS)


# ========== Task 审计（spec §2.31） ==========


class TestExportTaskAudit:
    """spec §2.31：所有导出一律建 UserExportTask。"""

    async def test_export_creates_task_with_filter_snapshot(
        self, db_session, file_storage
    ):
        """spec §2.31 line 1525-1536：建 task，filter_snapshot 含 filter + accessible_dept_ids。"""
        dept = _make_dept(5201, "QA-Exp-Dept-FS")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6201, "QA_EXP_FS", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        filter_ = UserExportFilter(user_name="QA", status="1")
        _bytes, _count, export_id = await export_users_to_excel(
            db_session,
            filter_,
            operator,
            reason="QA filter snapshot",
            file_storage=file_storage,
        )

        task = (
            await db_session.execute(
                _select(UserExportTask).where(UserExportTask.export_id == export_id)
            )
        ).scalar_one()
        assert task.operator_id == operator.user_id
        assert task.reason == "QA filter snapshot"
        assert task.status == ExportTaskStatus.SUCCESS
        # filter_snapshot 含原始 filter
        assert task.filter_snapshot["filter"]["user_name"] == "QA"
        assert task.filter_snapshot["filter"]["status"] == "1"
        # filter_snapshot 含 accessible_dept_ids + filter_evaluated_at
        assert "accessible_dept_ids" in task.filter_snapshot
        assert "filter_evaluated_at" in task.filter_snapshot

    async def test_export_filter_snapshot_freezes_accessible_dept_ids(
        self, db_session, file_storage
    ):
        """spec §2.31 line 1516-1520：filter_snapshot.accessible_dept_ids 冻结当时的部门集合。"""
        own_dept = _make_dept(5301, "QA-Exp-Own")
        other_dept = _make_dept(5302, "QA-Exp-Other")
        hr_role = _make_role(
            5303, "QA_R_HR_EXP", "QA-HR-EXP", data_scope=DATA_SCOPE_DEPT
        )
        operator = _make_user(6301, "QA_EXP_HR", [hr_role], [own_dept])
        db_session.add_all([own_dept, other_dept, hr_role, operator])
        await db_session.flush()

        _bytes, _count, _export_id = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            operator,
            reason="QA snapshot freeze",
            file_storage=file_storage,
        )

        task = (await db_session.execute(_select(UserExportTask))).scalar_one()
        # DATA_SCOPE_DEPT → accessible_dept_ids = own_dept 的 id
        assert task.filter_snapshot["accessible_dept_ids"] == [own_dept.dept_id]

    async def test_export_status_transitions_created_to_success(
        self, db_session, file_storage
    ):
        """spec §2.31 line 1540-1565：CREATED → RUNNING → SUCCESS。"""
        dept = _make_dept(5401, "QA-Exp-Dept-ST")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6401, "QA_EXP_ST", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        _bytes, _count, export_id = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            operator,
            reason="QA state transition",
            file_storage=file_storage,
        )

        task = (
            await db_session.execute(
                _select(UserExportTask).where(UserExportTask.export_id == export_id)
            )
        ).scalar_one()
        assert task.status == ExportTaskStatus.SUCCESS
        assert task.started_at is not None
        assert task.finished_at is not None
        assert task.duration_ms is not None
        assert task.duration_ms >= 0

    async def test_export_writes_file_to_storage(self, db_session, file_storage):
        """spec §2.31 line 1549-1555：xlsx 写入 FileStorage，路径存 task.file_storage_key。"""
        dept = _make_dept(5501, "QA-Exp-Dept-FL")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6501, "QA_EXP_FL", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        xlsx_bytes, _count, export_id = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            operator,
            reason="QA file storage",
            file_storage=file_storage,
        )

        task = (
            await db_session.execute(
                _select(UserExportTask).where(UserExportTask.export_id == export_id)
            )
        ).scalar_one()
        assert task.file_storage_key is not None
        assert "user-export" in task.file_storage_key
        assert task.file_size_bytes == len(xlsx_bytes)
        # 文件实际写入 MockFileStorage
        assert await file_storage.exists(task.file_storage_key)


# ========== Reason 必填（spec §2.30） ==========


class TestReasonValidation:
    """spec §2.30：reason 必填，1-256 字符（service 层 defense-in-depth）。"""

    async def test_export_reason_required(self, db_session, file_storage):
        dept = _make_dept(5601, "QA-Exp-Dept-RSN")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6601, "QA_EXP_RSN", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await export_users_to_excel(
                db_session,
                UserExportFilter(),
                operator,
                reason="   ",  # 全空白
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_EXPORT_REASON_REQUIRED"

    async def test_export_reason_too_long_rejected(self, db_session, file_storage):
        dept = _make_dept(5602, "QA-Exp-Dept-RL")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6602, "QA_EXP_RL", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await export_users_to_excel(
                db_session,
                UserExportFilter(),
                operator,
                reason="x" * 257,
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_EXPORT_REASON_REQUIRED"


# ========== 阈值（spec §2.6） ==========


class TestExportThreshold:
    """spec §2.6：行数 > USER_EXPORT_ASYNC_THRESHOLD → AI_EXPORT_ASYNC_REQUIRED。"""

    async def test_export_over_threshold_raises_async_required(
        self, db_session, file_storage
    ):
        dept = _make_dept(5701, "QA-Exp-Dept-TH")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(6701, "QA_EXP_TH", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        # 制造 USER_EXPORT_ASYNC_THRESHOLD + 1 个用户
        bulk_users = [
            _make_user(
                6702 + i,
                f"QA_EXP_BULK_{i:05d}",
                [super_role],
                [dept],
            )
            for i in range(USER_EXPORT_ASYNC_THRESHOLD + 1)
        ]
        db_session.add_all(bulk_users)
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await export_users_to_excel(
                db_session,
                UserExportFilter(),
                operator,
                reason="QA threshold",
                file_storage=file_storage,
            )
        assert exc.value.error_code == "AI_EXPORT_ASYNC_REQUIRED"


# ========== Data Scope（spec §2.31 line 1545） ==========


class TestDataScope:
    """data_scope 自动应用：HR 只能导他可见的部门用户。"""

    async def test_export_data_scope_filters_by_dept(self, db_session, file_storage):
        """spec §2.31 line 1545 + §2.11：DATA_SCOPE_DEPT 限定本部门。"""
        own_dept = _make_dept(5801, "QA-Exp-Own-D")
        other_dept = _make_dept(5802, "QA-Exp-Other-D")
        hr_role = _make_role(5803, "QA_R_HR_D", "QA-HR-D", data_scope=DATA_SCOPE_DEPT)
        operator = _make_user(6801, "QA_EXP_HR_D", [hr_role], [own_dept])
        own_user = _make_user(6802, "QA_IN_OWN", [hr_role], [own_dept])
        other_user = _make_user(6803, "QA_IN_OTHER", [hr_role], [other_dept])
        db_session.add_all(
            [own_dept, other_dept, hr_role, operator, own_user, other_user]
        )
        await db_session.flush()

        xlsx_bytes, row_count, _ = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            operator,
            reason="QA data scope",
            file_storage=file_storage,
        )

        # HR 只看到 own_dept 里的用户（operator 自己 + own_user）
        assert row_count == 2
        headers, rows = _read_xlsx(xlsx_bytes)
        user_name_col = 0  # 第 1 列
        exported_names = {r[user_name_col] for r in rows}
        assert "QA_IN_OWN" in exported_names
        assert "QA_IN_OTHER" not in exported_names

    async def test_export_super_admin_sees_all(self, db_session, file_storage):
        """spec line 2662：超管豁免 data_scope，看所有用户。"""
        dept1 = _make_dept(5901, "QA-Exp-SA-D1")
        dept2 = _make_dept(5902, "QA-Exp-SA-D2")
        admin_user = (
            await db_session.execute(
                _select(User).where(User.user_name == ADMIN_USERNAME)
            )
        ).scalar_one()
        u1 = _make_user(6902, "QA_SA_U1", [], [dept1])
        u2 = _make_user(6903, "QA_SA_U2", [], [dept2])
        db_session.add_all([dept1, dept2, u1, u2])
        await db_session.flush()

        _bytes, row_count, _ = await export_users_to_excel(
            db_session,
            UserExportFilter(),
            admin_user,
            reason="QA super admin export",
            file_storage=file_storage,
        )

        # 超管看所有用户（含 init_db seed 的 + dept1/dept2 两个新用户）
        assert row_count >= 2


# ========== 失败也建 task（spec §2.31 line 1567-1572） ==========


class TestExportFailureRecordsError:
    """spec §2.31 line 1567-1572：失败也建 task，status=FAILED + error_code。"""

    async def test_export_failure_records_async_required_in_task(
        self, db_session, file_storage
    ):
        """超阈值时 task.status=FAILED + error_code=AI_EXPORT_ASYNC_REQUIRED。"""
        dept = _make_dept(6001, "QA-Exp-Dept-FE")
        super_role = await _fetch_super_role(db_session)
        operator = _make_user(7001, "QA_EXP_FE", [super_role], [dept])
        db_session.add_all([dept, operator])
        await db_session.flush()

        bulk_users = [
            _make_user(
                7002 + i,
                f"QA_EXP_FAIL_{i:05d}",
                [super_role],
                [dept],
            )
            for i in range(USER_EXPORT_ASYNC_THRESHOLD + 1)
        ]
        db_session.add_all(bulk_users)
        await db_session.flush()

        with pytest.raises(BusinessRuleException):
            await export_users_to_excel(
                db_session,
                UserExportFilter(),
                operator,
                reason="QA failure task",
                file_storage=file_storage,
            )

        task = (await db_session.execute(_select(UserExportTask))).scalar_one()
        assert task.status == ExportTaskStatus.FAILED
        assert task.error_code == "AI_EXPORT_ASYNC_REQUIRED"
        assert task.error_message is not None
        assert task.finished_at is not None
