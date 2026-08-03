"""import_validator 单测（Task 4 + Task 5 + Task 6）。

覆盖：
- resolve_dept 5 用例（spec §2.17）：名称 / 路径 / 重名 / 不存在 / 路径段断
- resolve_role_input 4 用例（spec §2.18）：code / name / 混合去重 / 未匹配
- check_permission_boundary 3 用例（spec §2.15）：越界 / 超管豁免 / 错误提示含名

依赖 db_session outer-transaction fixture（不落库）。
"""

import pytest
from sqlalchemy import select as _select

from app.constants import ADMIN_USERNAME, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import BusinessRuleException
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.user.import_validator import (
    check_permission_boundary,
    resolve_dept,
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
    """禁用部门不应被命中（spec §2.17 line 451 未明确，但安全原则：禁止把用户分到停用部门）。"""

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
    """构造 user 并关联 roles（spec §2.15 测试用）。"""
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


class TestCheckPermissionBoundary:
    """check_permission_boundary 3 用例（spec §2.15 / line 2661-2663）。

    注意：R_SUPER / admin 是 init_db.py seed 数据。本测试组通过 select
    已有 R_SUPER 角色 + 关联到测试 user，避免 UniqueViolation。
    """

    async def test_permission_boundary_role_out_of_scope(self, db_session):
        """spec 用例 1：HR 拥有 QA_R_HR，给用户分配 QA_R_FORBIDDEN → 越界。"""
        hr_role = _make_role(2001, "QA_R_HR_OOS", "QA人事-OOS")
        forbidden_role = _make_role(2002, "QA_R_FORBIDDEN", "QA禁止分配")
        db_session.add_all([hr_role, forbidden_role])
        await db_session.flush()

        operator = _make_user(9001, "QA_HR_OOS_User", [hr_role])
        record = _make_record(10, "QA_Newbie1", "QA_R_FORBIDDEN")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_permission_boundary(db_session, [record], operator)

        assert len(failed) == 1
        assert failed[0].row_num == 10
        assert failed[0].field == "role_input"
        assert failed[0].error_code == "AI_IMPORT_ROLE_OUT_OF_SCOPE"
        assert failed[0].value == "QA_R_FORBIDDEN"

    async def test_permission_boundary_super_admin_bypass(self, db_session):
        """spec 用例 2：超管（拥有 R_SUPER）导入可分配任意角色（豁免）。"""
        # 复用 init_db.py seed 的 R_SUPER 角色（避免 UniqueViolation）
        super_role = (
            await db_session.execute(
                _select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalar_one()
        any_role = _make_role(2011, "QA_R_ANY", "QA任意角色")
        db_session.add(any_role)
        await db_session.flush()

        operator = _make_user(9010, "QA_Super_User", [super_role])
        record = _make_record(20, "QA_New_SA", "QA_R_ANY")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_permission_boundary(db_session, [record], operator)
        assert failed == []

    async def test_permission_boundary_error_lists_role_names(self, db_session):
        """spec 用例 3：错误提示含角色名（非 ID）。

        HR 越权分配「QA财务-X」+「QA系统管理员-X」两个角色 → reason 应含两个名字。
        """
        hr_role = _make_role(2020, "QA_R_HR_NAMES", "QA人事-N")
        fin_role = _make_role(2021, "QA_R_FIN_NAMES", "QA财务-X")
        sys_role = _make_role(2022, "QA_R_SYS_NAMES", "QA系统管理员-X")
        db_session.add_all([hr_role, fin_role, sys_role])
        await db_session.flush()

        operator = _make_user(9020, "QA_HR_N", [hr_role])
        record = _make_record(30, "QA_Newbie2", "QA_R_FIN_NAMES,QA_R_SYS_NAMES")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_permission_boundary(db_session, [record], operator)
        assert len(failed) == 1
        assert failed[0].error_code == "AI_IMPORT_ROLE_OUT_OF_SCOPE"
        # reason 含角色名（不是 ID）
        assert "QA财务-X" in failed[0].reason
        assert "QA系统管理员-X" in failed[0].reason
        # 不含 role_id 数字
        assert "2021" not in failed[0].reason
        assert "2022" not in failed[0].reason

    async def test_permission_boundary_admin_username_bypass(self, db_session):
        """user_name='admin' 即使无 R_SUPER 角色也豁免（is_super_admin 双判）。"""
        # 复用 init_db.py seed 的 admin user（避免 user_name UniqueViolation）
        admin_user = (
            await db_session.execute(
                _select(User).where(User.user_name == ADMIN_USERNAME)
            )
        ).scalar_one()
        any_role = _make_role(2030, "QA_R_ANY_ADM", "QA任意-ADM")
        db_session.add(any_role)
        await db_session.flush()

        record = _make_record(40, "QA_New_ADM", "QA_R_ANY_ADM")

        failed = await check_permission_boundary(db_session, [record], admin_user)
        assert failed == []

    async def test_permission_boundary_all_in_scope_returns_empty(self, db_session):
        """所有请求角色都在 operator scope 内 → 返回空 list。"""
        r1 = _make_role(2040, "QA_R_OK1", "QA允许1")
        r2 = _make_role(2041, "QA_R_OK2", "QA允许2")
        db_session.add_all([r1, r2])
        await db_session.flush()

        operator = _make_user(9040, "QA_Manager", [r1, r2])
        record = _make_record(50, "QA_Newbie3", "QA_R_OK1,QA_R_OK2")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_permission_boundary(db_session, [record], operator)
        assert failed == []

    async def test_permission_boundary_role_not_found_treated_as_failed_row(
        self, db_session
    ):
        """role_input 反查失败 → FailedRow(error_code=AI_IMPORT_ROLE_NOT_FOUND)。"""
        hr_role = _make_role(2050, "QA_R_HR_NF", "QA人事-NF")
        db_session.add(hr_role)
        await db_session.flush()

        operator = _make_user(9050, "QA_HR_NF", [hr_role])
        record = _make_record(60, "QA_Newbie4", "QA_Not_Exist_Role_NF")
        db_session.add(operator)
        await db_session.flush()

        failed = await check_permission_boundary(db_session, [record], operator)
        assert len(failed) == 1
        assert failed[0].error_code == "AI_IMPORT_ROLE_NOT_FOUND"

    async def test_permission_boundary_partial_failure_only_others_succeed(
        self, db_session
    ):
        """多行 records：部分越界 / 部分合法 → 只越界行进 failed_rows。"""
        hr_role = _make_role(2060, "QA_R_HR_PF", "QA人事-PF")
        dev_role = _make_role(2061, "QA_R_DEV_PF", "QA开发者-PF")
        db_session.add_all([hr_role, dev_role])
        await db_session.flush()

        operator = _make_user(9060, "QA_HR_PF", [hr_role])  # 只有 HR
        records = [
            _make_record(70, "QA_ok1", "QA_R_HR_PF"),  # OK
            _make_record(71, "QA_bad1", "QA_R_DEV_PF"),  # 越权
            _make_record(72, "QA_ok2", "QA_R_HR_PF"),  # OK
        ]
        db_session.add(operator)
        await db_session.flush()

        failed = await check_permission_boundary(db_session, records, operator)
        assert len(failed) == 1
        assert failed[0].row_num == 71
        assert failed[0].error_code == "AI_IMPORT_ROLE_OUT_OF_SCOPE"
