"""import_validator 单测（Task 4，spec §2.17）。

覆盖 resolve_dept 5 用例（spec line 2635-2639）：
- 名称模式：唯一命中 / 重名 / 不存在
- 路径模式：逐级命中 / 某段不存在

依赖 db_session outer-transaction fixture（不落库）。
"""

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.system.models.dept import Dept
from app.modules.system.user.import_validator import resolve_dept


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
