"""import_validator 单测（Task 4 + Task 5，spec §2.17 / §2.18）。

覆盖：
- resolve_dept 5 用例（spec line 2635-2639）：名称 / 路径 / 重名 / 不存在 / 路径段断
- resolve_role_input 4 用例（spec line 2640-2643）：code / name / 混合去重 / 未匹配

依赖 db_session outer-transaction fixture（不落库）。
"""

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.user.import_validator import (
    resolve_dept,
    resolve_role_input,
)


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
