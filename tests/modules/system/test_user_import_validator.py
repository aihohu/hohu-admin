"""import_validator 行为测试。

覆盖：
- resolve_dept：名称、路径、重名、不存在和路径中断
- resolve_role_input：code、name、混合去重和未匹配
- check_dept_data_scope：self、dept_and_sub 和越界
- resolve_existing_user 与 classify_sync_action：
  CREATE_ONLY / UPDATE_PROFILE / FULL_SYNC / NULL 兜底

依赖 db_session outer-transaction fixture（不落库）。
"""

import pytest
from sqlalchemy import insert
from sqlalchemy import select as _select

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import BusinessRuleException
from app.core.security import get_password_hash
from app.db.base import role_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.user.constants import EmployeeNoSyncMode
from app.modules.system.user.import_validator import (
    SyncAction,
    check_dept_data_scope,
    classify_sync_action,
    resolve_dept,
    resolve_existing_user,
    resolve_role_input,
)
from app.modules.system.user.schemas import UserImportRecord


def _make_dept(
    dept_id: int,
    name: str,
    parent_id: int | None = None,
    status: str = "1",
) -> Dept:
    return Dept(
        dept_id=dept_id,
        parent_id=parent_id,
        dept_name=name,
        order_num=0,
        status=status,
    )


class TestResolveDeptNameMode:
    """名称模式：dept_input 不含 / 。"""

    async def test_resolve_dept_name_unique(self, db_session):
        """spec 用例 1：唯一名命中 → dept_id。"""
        db_session.add(_make_dept(101, "QA-Name-Unique"))
        await db_session.flush()

        result = await resolve_dept(db_session, "QA-Name-Unique")
        assert result == 101

    async def test_resolve_dept_name_duplicate(self, db_session):
        """spec 用例 2：重名 → AI_IMPORT_DEPT_DUPLICATE。"""
        db_session.add_all(
            [
                _make_dept(201, "QA-Dup-Dept", parent_id=None),
                _make_dept(202, "QA-Dup-Dept", parent_id=999),
            ]
        )
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await resolve_dept(db_session, "QA-Dup-Dept")
        assert exc.value.error_code == "AI_IMPORT_DEPT_DUPLICATE"

    async def test_resolve_dept_not_found(self, db_session):
        """spec 用例 4：无匹配 → AI_IMPORT_DEPT_NOT_FOUND。"""
        with pytest.raises(BusinessRuleException) as exc:
            await resolve_dept(db_session, "QA-Definitely-Not-Exists-12345")
        assert exc.value.error_code == "AI_IMPORT_DEPT_NOT_FOUND"


class TestResolveDeptPathMode:
    """路径模式：dept_input 含 /。"""

    async def test_resolve_dept_path_mode(self, db_session):
        """spec 用例 3：3 级路径逐级查 → 末级 dept_id。"""
        db_session.add_all(
            [
                _make_dept(301, "QA-Path-Root", parent_id=None),
                _make_dept(302, "QA-Path-Mid", parent_id=301),
                _make_dept(303, "QA-Path-Leaf", parent_id=302),
                # 干扰项：另一棵树的同名 leaf（路径模式应跳过）
                _make_dept(310, "QA-Path-Other", parent_id=None),
                _make_dept(311, "QA-Path-Leaf", parent_id=310),
            ]
        )
        await db_session.flush()

        result = await resolve_dept(db_session, "QA-Path-Root/QA-Path-Mid/QA-Path-Leaf")
        assert result == 303

    async def test_resolve_dept_path_segment_missing(self, db_session):
        """spec 用例 5：路径某段不存在 → AI_IMPORT_DEPT_PATH_NOT_FOUND。"""
        db_session.add(_make_dept(401, "QA-Missing-Root", parent_id=None))
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await resolve_dept(db_session, "QA-Missing-Root/QA-Not-Exist/QA-Leaf")
        assert exc.value.error_code == "AI_IMPORT_DEPT_PATH_NOT_FOUND"

    async def test_resolve_dept_path_with_whitespace_segments(self, db_session):
        """路径段含空格：strip 后命中。"""
        db_session.add_all(
            [
                _make_dept(501, "QA-Ws-Root", parent_id=None),
                _make_dept(502, "QA-Ws-Child", parent_id=501),
            ]
        )
        await db_session.flush()

        result = await resolve_dept(db_session, " QA-Ws-Root / QA-Ws-Child ")
        assert result == 502


class TestResolveDeptStatusFilter:
    """禁用部门不应被命中，避免把用户分配到停用组织。"""

    async def test_resolve_dept_skips_disabled_name_mode(self, db_session):
        """名称模式下：禁用部门等于不存在。"""
        db_session.add(_make_dept(601, "QA-Disabled-Name", status="2"))
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await resolve_dept(db_session, "QA-Disabled-Name")
        assert exc.value.error_code == "AI_IMPORT_DEPT_NOT_FOUND"

    async def test_resolve_dept_skips_disabled_path_mode(self, db_session):
        """路径模式下：禁用部门段视为不存在。"""
        db_session.add_all(
            [
                _make_dept(701, "QA-Disabled-Root", parent_id=None),
                _make_dept(702, "QA-Disabled-Mid", parent_id=701, status="2"),
            ]
        )
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await resolve_dept(db_session, "QA-Disabled-Root/QA-Disabled-Mid")
        assert exc.value.error_code == "AI_IMPORT_DEPT_PATH_NOT_FOUND"


def _make_role(
    role_id: int,
    code: str,
    name: str,
    status: str = "1",
    data_scope: str = "1",
) -> Role:
    return Role(
        role_id=role_id,
        role_code=code,
        role_name=name,
        data_scope=data_scope,
        status=status,
    )


class TestResolveRoleInput:
    """resolve_role_input 4 用例（spec line 2640-2643）。"""

    async def test_resolve_role_input_by_code(self, db_session):
        """spec 用例 1：'R_DEV' (code) → role_id。"""
        db_session.add(_make_role(1001, "QA_R_DEV", "QA开发者"))
        await db_session.flush()

        result = await resolve_role_input(db_session, "QA_R_DEV")
        assert result == [1001]

    async def test_resolve_role_input_by_name(self, db_session):
        """spec 用例 2：'QA开发者' (name) → role_id。"""
        db_session.add(_make_role(1002, "QA_R_PM", "QA产品经理"))
        await db_session.flush()

        result = await resolve_role_input(db_session, "QA产品经理")
        assert result == [1002]

    async def test_resolve_role_input_mixed_dedup(self, db_session):
        """spec 用例 3：混合 code+name 输入 + 同角色写两次去重。

        'QA_R_QA,QA测试,QA_R_QA' → 同一 role_id 只出现一次。
        """
        db_session.add_all(
            [
                _make_role(1010, "QA_R_QA", "QA测试"),
                _make_role(1011, "QA_R_OPS", "QA运维"),
            ]
        )
        await db_session.flush()

        result = await resolve_role_input(db_session, "QA_R_QA, QA测试, QA_R_OPS")
        assert sorted(result) == [1010, 1011]

    async def test_resolve_role_input_not_found(self, db_session):
        """spec 用例 4：未匹配 → AI_IMPORT_ROLE_NOT_FOUND（含未匹配项）。"""
        with pytest.raises(BusinessRuleException) as exc:
            await resolve_role_input(db_session, "QA_Not_Exist_Role")
        assert exc.value.error_code == "AI_IMPORT_ROLE_NOT_FOUND"
        assert "QA_Not_Exist_Role" in str(exc.value)

    async def test_resolve_role_input_skips_disabled(self, db_session):
        """禁用角色（status='2'）一律视为不存在（与 dept 一致）。"""
        db_session.add(_make_role(1020, "QA_R_DISABLED", "QA禁用角色", status="2"))
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await resolve_role_input(db_session, "QA_R_DISABLED")
        assert exc.value.error_code == "AI_IMPORT_ROLE_NOT_FOUND"

    async def test_resolve_role_input_partial_match_reports_unmatched(self, db_session):
        """部分匹配：未匹配项进入异常信息（spec line 499 含 remaining）。"""
        db_session.add(_make_role(1030, "QA_R_OK", "QA存在角色"))
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await resolve_role_input(db_session, "QA_R_OK,QA_Not_Exist")
        assert exc.value.error_code == "AI_IMPORT_ROLE_NOT_FOUND"
        assert "QA_Not_Exist" in str(exc.value)
        # 已匹配的 QA_R_OK 不应进入异常信息
        assert "QA_R_OK" not in str(exc.value)


def _make_user(
    user_id: int,
    user_name: str,
    roles: list[Role],
    status: str = "1",
) -> User:
    """构造用户并关联角色。"""
    return User(
        user_id=user_id,
        user_name=user_name,
        hashed_password="x",
        status=status,
        roles=roles,
    )


def _make_record(
    row_num: int,
    user_name: str,
    role_input: str | None,
) -> UserImportRecord:
    return UserImportRecord(
        row_num=row_num,
        user_name=user_name,
        dept_input="QA-Dept-Placeholder",
        role_input=role_input,
    )


def _make_dept_for_scope(
    dept_id: int,
    name: str,
    parent_id: int | None = None,
    ancestors: str | None = None,
) -> Dept:
    """带 ancestors 字段（get_dept_and_sub_ids 用），用于 data_scope 测试。"""
    return Dept(
        dept_id=dept_id,
        parent_id=parent_id,
        ancestors=ancestors,
        dept_name=name,
        order_num=0,
        status="1",
    )


def _make_scoped_record(
    row_num: int, user_name: str, dept_input: str
) -> UserImportRecord:
    """dept scope 测试专用 record（role_input 留空，与 scope 无关）。"""
    return UserImportRecord(
        row_num=row_num,
        user_name=user_name,
        dept_input=dept_input,
        role_input=None,
    )


class TestCheckDeptDataScope:
    """覆盖 check_dept_data_scope 的范围判断。

    测试场景：
    - DATA_SCOPE_SELF → 空 accessible_dept_ids → 任何 dept 都越界
    - DATA_SCOPE_DEPT_AND_SUB → 子部门可访问（验证 ancestors like）
    - DATA_SCOPE_DEPT → 其他部门越界
    - DATA_SCOPE_ALL / 超管 → 跳过
    - resolve_dept 失败 → FailedRow 携带对应 error_code
    """

    async def test_dept_data_scope_self_only_blocks_all(self, db_session):
        """spec 用例 1：DATA_SCOPE_SELF → accessible_dept_ids=set()，全越界。"""
        # 准备：user 在 dept 8001，role 是 DATA_SCOPE_SELF
        dept = _make_dept_for_scope(8001, "QA-Self-Dept")
        role = _make_role(3001, "QA_R_SELF", "QA-self-role", data_scope=DATA_SCOPE_SELF)
        db_session.add_all([dept, role])
        await db_session.flush()

        operator = _make_user(9100, "QA_Self_User", [role])
        operator.depts = [dept]
        record = _make_scoped_record(80, "QA_New_Self", "QA-Self-Dept")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_dept_data_scope(db_session, [record], operator)
        assert len(failed) == 1
        assert failed[0].row_num == 80
        assert failed[0].field == "dept_input"
        assert failed[0].error_code == "AI_IMPORT_DEPT_OUT_OF_SCOPE"

    async def test_dept_data_scope_dept_and_sub_allows_sub(self, db_session):
        """spec 用例 2：DATA_SCOPE_DEPT_AND_SUB → 子部门用户可导入。

        ancestors 字段：get_dept_and_sub_ids 用 func.concat like 匹配。
        """
        # 部门树：root(8100) > mid(8101) > leaf(8102)
        root = _make_dept_for_scope(8100, "QA-Sub-Root", ancestors="0")
        mid = _make_dept_for_scope(
            8101, "QA-Sub-Mid", parent_id=8100, ancestors="0,8100"
        )
        leaf = _make_dept_for_scope(
            8102, "QA-Sub-Leaf", parent_id=8101, ancestors="0,8100,8101"
        )
        # 干扰：另一棵树
        other = _make_dept_for_scope(8199, "QA-Sub-Other", ancestors="0")
        role = _make_role(
            3002, "QA_R_DS", "QA-ds-role", data_scope=DATA_SCOPE_DEPT_AND_SUB
        )
        db_session.add_all([root, mid, leaf, other, role])
        await db_session.flush()

        operator = _make_user(9101, "QA_DS_Manager", [role])
        operator.depts = [mid]  # 在 mid 部门 → mid + leaf 可见，root / other 不可见
        records = [
            _make_scoped_record(81, "QA_New_Mid", "QA-Sub-Mid"),  # OK
            _make_scoped_record(82, "QA_New_Leaf", "QA-Sub-Leaf"),  # OK（子部门）
            _make_scoped_record(83, "QA_New_Other", "QA-Sub-Other"),  # 越界
        ]
        db_session.add(operator)
        await db_session.flush()

        failed = await check_dept_data_scope(db_session, records, operator)
        assert len(failed) == 1
        assert failed[0].row_num == 83
        assert failed[0].error_code == "AI_IMPORT_DEPT_OUT_OF_SCOPE"

    async def test_dept_data_scope_violation_dept_scope(self, db_session):
        """spec 用例 3：DATA_SCOPE_DEPT 操作人导入其他部门 → 越界。"""
        own_dept = _make_dept_for_scope(8201, "QA-Own-Dept")
        other_dept = _make_dept_for_scope(8202, "QA-Other-Dept")
        role = _make_role(3003, "QA_R_D", "QA-d-role", data_scope=DATA_SCOPE_DEPT)
        db_session.add_all([own_dept, other_dept, role])
        await db_session.flush()

        operator = _make_user(9102, "QA_Dept_Mgr", [role])
        operator.depts = [own_dept]
        records = [
            _make_scoped_record(91, "QA_Ok", "QA-Own-Dept"),  # OK
            _make_scoped_record(92, "QA_Bad", "QA-Other-Dept"),  # 越界
        ]
        db_session.add(operator)
        await db_session.flush()

        failed = await check_dept_data_scope(db_session, records, operator)
        assert len(failed) == 1
        assert failed[0].row_num == 92
        assert failed[0].error_code == "AI_IMPORT_DEPT_OUT_OF_SCOPE"

    async def test_multi_role_dept_and_custom_scopes_are_unioned(self, db_session):
        """Every enabled role contributes to the import department boundary."""
        own_dept = _make_dept_for_scope(8251, "QA-Union-Own")
        custom_dept = _make_dept_for_scope(8252, "QA-Union-Custom")
        dept_role = _make_role(
            3051,
            "QA_R_UNION_DEPT",
            "QA-union-dept-role",
            data_scope=DATA_SCOPE_DEPT,
        )
        custom_role = _make_role(
            3052,
            "QA_R_UNION_CUSTOM",
            "QA-union-custom-role",
            data_scope=DATA_SCOPE_CUSTOM,
        )
        db_session.add_all([own_dept, custom_dept, dept_role, custom_role])
        await db_session.flush()
        await db_session.execute(
            insert(role_depts).values(
                role_id=custom_role.role_id,
                dept_id=custom_dept.dept_id,
            )
        )

        operator = _make_user(
            9151,
            "QA_Union_Manager",
            [dept_role, custom_role],
        )
        operator.depts = [own_dept]
        records = [
            _make_scoped_record(95, "QA_Union_Own", "QA-Union-Own"),
            _make_scoped_record(96, "QA_Union_Custom", "QA-Union-Custom"),
        ]
        db_session.add(operator)
        await db_session.flush()

        assert (
            await check_dept_data_scope(
                db_session,
                records,
                operator,
            )
            == []
        )

    async def test_dept_data_scope_super_admin_bypass(self, db_session):
        """超管豁免（accessible_dept_ids=None）。"""
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        any_dept = _make_dept_for_scope(8301, "QA-SA-Dept")
        db_session.add(any_dept)
        await db_session.flush()

        operator = _make_user(9103, "QA_SA_Op", [super_role])
        record = _make_scoped_record(100, "QA_New_SA", "QA-SA-Dept")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_dept_data_scope(db_session, [record], operator)
        assert failed == []

    async def test_dept_data_scope_all_scope_bypass(self, db_session):
        """DATA_SCOPE_ALL 角色（非超管）也豁免。"""
        all_role = _make_role(
            3005, "QA_R_ALL", "QA-all-role", data_scope=DATA_SCOPE_ALL
        )
        any_dept = _make_dept_for_scope(8401, "QA-All-Dept")
        db_session.add_all([all_role, any_dept])
        await db_session.flush()

        operator = _make_user(9104, "QA_All_Op", [all_role])
        record = _make_scoped_record(110, "QA_New_All", "QA-All-Dept")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_dept_data_scope(db_session, [record], operator)
        assert failed == []

    async def test_dept_data_scope_dept_resolve_failure_keeps_error_code(
        self, db_session
    ):
        """dept_input 反查失败 → FailedRow 携带原 error_code（不混淆为 OUT_OF_SCOPE）。"""
        dept = _make_dept_for_scope(8501, "QA-Real-Dept")
        role = _make_role(3006, "QA_R_DF", "QA-df-role", data_scope=DATA_SCOPE_DEPT)
        db_session.add_all([dept, role])
        await db_session.flush()

        operator = _make_user(9105, "QA_DF_Op", [role])
        operator.depts = [dept]
        # 构造重名部门。
        db_session.add_all(
            [
                _make_dept_for_scope(8502, "QA-Dup"),
                _make_dept_for_scope(
                    8503, "QA-Dup", parent_id=9999, ancestors="0,9999"
                ),
            ]
        )
        record = _make_scoped_record(120, "QA_New_DF", "QA-Dup")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_dept_data_scope(db_session, [record], operator)
        assert len(failed) == 1
        # 应该是 DUPLICATE 而不是 OUT_OF_SCOPE（resolve 失败保留原 error_code）
        assert failed[0].error_code == "AI_IMPORT_DEPT_DUPLICATE"


def _make_existing_user(
    user_id: int,
    user_name: str,
    employee_no: str | None = None,
) -> User:
    """构造已存在用户（resolve_existing_user 测试用）。"""
    return User(
        user_id=user_id,
        user_name=user_name,
        employee_no=employee_no,
        hashed_password=get_password_hash("x"),
        status="1",
    )


def _make_record_with_emp(
    row_num: int,
    user_name: str,
    employee_no: str | None,
) -> UserImportRecord:
    return UserImportRecord(
        row_num=row_num,
        user_name=user_name,
        employee_no=employee_no,
        dept_input="QA-Dept-Placeholder",
        role_input=None,
    )


class TestResolveExistingUser:
    """resolve_existing_user 数据库行为测试。

    匹配顺序：employee_no（优先）→ user_name（兜底）。
    """

    async def test_resolve_existing_user_matches_by_employee_no(self, db_session):
        """employee_no 命中 → 返回 user, matched=True。"""
        existing = _make_existing_user(11001, "QA_Old_Name", employee_no="QA_E001")
        db_session.add(existing)
        await db_session.flush()

        record = _make_record_with_emp(200, "QA_New_Name", "QA_E001")
        user, matched = await resolve_existing_user(db_session, record)

        assert user is not None
        assert user.user_id == 11001
        assert matched is True

    async def test_resolve_existing_user_falls_back_to_username(self, db_session):
        """employee_no 为 NULL + user_name 命中 → 返回 user, matched=False。"""
        existing = _make_existing_user(11002, "QA_FB_User", employee_no=None)
        db_session.add(existing)
        await db_session.flush()

        record = _make_record_with_emp(201, "QA_FB_User", None)
        user, matched = await resolve_existing_user(db_session, record)

        assert user is not None
        assert user.user_id == 11002
        assert matched is False

    async def test_resolve_existing_user_employee_no_miss_username_hit(
        self, db_session
    ):
        """employee_no 未命中但 user_name 命中 → 兜底按 user_name，matched=False。"""
        # 不同 employee_no 的同 user_name 不可能（user_name UNIQUE），构造 user_name
        # 命中场景：record.employee_no 是另一个值，user_name 命中
        existing = _make_existing_user(11003, "QA_Shared", employee_no="QA_OLD")
        db_session.add(existing)
        await db_session.flush()

        record = _make_record_with_emp(202, "QA_Shared", "QA_NEW_EMP")
        user, matched = await resolve_existing_user(db_session, record)

        # employee_no 没命中，但 user_name 命中 → 兜底
        assert user is not None
        assert user.user_id == 11003
        assert matched is False

    async def test_resolve_existing_user_no_match_returns_none(self, db_session):
        """employee_no 和 user_name 都未命中 → (None, False)。"""
        record = _make_record_with_emp(203, "QA_No_Match", "QA_No_Such")
        user, matched = await resolve_existing_user(db_session, record)
        assert user is None
        assert matched is False


class TestClassifySyncAction:
    """classify_sync_action 纯逻辑测试。

    spec 4 用例：
    - CREATE_ONLY + employee_no 命中 → REJECT
    - UPDATE_PROFILE + employee_no 命中 → UPDATE_SAFE
    - FULL_SYNC + employee_no 命中 → UPDATE_FULL
    - employee_no NULL → EXISTS_BY_USERNAME（与 sync_mode 无关）
    """

    def test_create_only_employee_no_match_rejects(self):
        """sync_mode=CREATE_ONLY + employee_no 命中 → REJECT_EMPLOYEE_NO_EXISTS。"""
        action = classify_sync_action(
            matched_by_employee_no=True,
            sync_mode=EmployeeNoSyncMode.CREATE_ONLY,
        )
        assert action == SyncAction.REJECT_EMPLOYEE_NO_EXISTS

    def test_update_profile_employee_no_match_safe_fields(self):
        """sync_mode=UPDATE_PROFILE + employee_no 命中 → UPDATE_SAFE（不动 user_name）。"""
        action = classify_sync_action(
            matched_by_employee_no=True,
            sync_mode=EmployeeNoSyncMode.UPDATE_PROFILE,
        )
        assert action == SyncAction.UPDATE_SAFE

    def test_full_sync_employee_no_match_full_fields(self):
        """sync_mode=FULL_SYNC + employee_no 命中 → UPDATE_FULL（含 user_name）。"""
        action = classify_sync_action(
            matched_by_employee_no=True,
            sync_mode=EmployeeNoSyncMode.FULL_SYNC,
        )
        assert action == SyncAction.UPDATE_FULL

    def test_employee_no_null_username_match_exists_by_username(self):
        """employee_no NULL → 不论 sync_mode 一律 EXISTS_BY_USERNAME（line 874）。"""
        for mode in EmployeeNoSyncMode:
            action = classify_sync_action(
                matched_by_employee_no=False,
                sync_mode=mode,
            )
            assert action == SyncAction.EXISTS_BY_USERNAME, (
                f"sync_mode={mode} 时 employee_no NULL 兜底应一律 EXISTS_BY_USERNAME"
            )
