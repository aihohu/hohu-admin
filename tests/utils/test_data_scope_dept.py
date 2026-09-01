"""_get_dept_and_sub_ids 锚定匹配测试。

回归：旧实现用 ancestors.like(f"%{did}%") 会数字子串误匹配，
比如 dept_id=12 会匹配 ancestors="0,123,..."（"123" 含 "12"），
导致 DEPT_AND_SUB 数据权限过度放行。

用大数字 ID 避开预置数据冲突。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.models.dept import Dept
from app.utils.data_scope import _get_dept_and_sub_ids
from tests.tenant_helpers import tenant_context

TENANT = tenant_context()


def _make_dept(*, dept_id: int, name: str, ancestors: str) -> Dept:
    return Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=dept_id,
        dept_name=name,
        ancestors=ancestors,
        order_num=0,
        status="1",
    )


async def test_no_substring_mismatch_d12_vs_d123(db_session: AsyncSession):
    """dept_id=...12 查子部门时，ancestors='0,...123' 的部门不应被误匹配。"""
    base = 900000000
    db_session.add_all(
        [
            _make_dept(dept_id=base + 12, name="D12", ancestors="0"),
            _make_dept(dept_id=base + 1, name="D1", ancestors="0"),
            # ancestors 含 "...12" 子串但不是 (base+12) 的子部门
            _make_dept(dept_id=base + 123, name="D123", ancestors=f"0,{base + 1}"),
            _make_dept(
                dept_id=base + 1234,
                name="D1234",
                ancestors=f"0,{base + 123}",
            ),
        ]
    )
    await db_session.flush()

    result = set(await _get_dept_and_sub_ids(db_session, [base + 12], tenant=TENANT))

    assert base + 12 in result
    # 关键回归断言：含 "...12" 子串但不是 (base+12) 子部门的，不应被返回
    assert base + 123 not in result
    assert base + 1234 not in result


async def test_returns_real_children_and_skips_lookalikes(
    db_session: AsyncSession,
):
    """真实子和孙应返回；ancestors 含子串但非真子的不应返回。"""
    base = 910000000
    db_session.add_all(
        [
            _make_dept(dept_id=base + 1, name="D1", ancestors="0"),
            _make_dept(dept_id=base + 2, name="D2", ancestors=f"0,{base + 1}"),
            _make_dept(
                dept_id=base + 3, name="D3", ancestors=f"0,{base + 1},{base + 2}"
            ),
            # 干扰项：ancestors 含 base+1 的子串但不是 (base+1) 的子部门
            _make_dept(dept_id=base + 10, name="D10", ancestors="0"),
            _make_dept(
                dept_id=base + 100,
                name="D_under_10",
                ancestors=f"0,{base + 10}",
            ),
        ]
    )
    await db_session.flush()

    result = set(await _get_dept_and_sub_ids(db_session, [base + 1], tenant=TENANT))

    # 真实子链：base+1 → base+2 → base+3
    assert {base + 1, base + 2, base + 3}.issubset(result)
    # 干扰项：ancestors 含 "base+1" 数字子串但不是真子
    assert base + 10 not in result
    assert base + 100 not in result


async def test_multiple_input_depts_returns_all_subtrees(db_session: AsyncSession):
    """多 dept_id 输入：每个 dept 的子树都应返回（保证批量查询不丢结果）。"""
    base = 930000000
    # 两棵独立子树，ancestors 是完整父链
    db_session.add_all(
        [
            _make_dept(dept_id=base + 1, name="A1", ancestors="0"),
            _make_dept(dept_id=base + 2, name="A2", ancestors=f"0,{base + 1}"),
            _make_dept(dept_id=base + 10, name="B1", ancestors="0"),
            _make_dept(dept_id=base + 20, name="B2", ancestors=f"0,{base + 10}"),
            _make_dept(
                dept_id=base + 30,
                name="B3",
                ancestors=f"0,{base + 10},{base + 20}",
            ),
            # B 子树最深：ancestors 必须含所有祖先
            _make_dept(
                dept_id=base + 100,
                name="X",
                ancestors=f"0,{base + 10},{base + 20},{base + 30}",
            ),
        ]
    )
    await db_session.flush()

    result = set(
        await _get_dept_and_sub_ids(
            db_session,
            [base + 1, base + 10],
            tenant=TENANT,
        )
    )

    # A 子树：base+1, base+2
    # B 子树：base+10, base+20, base+30, base+100
    expected = {base + 1, base + 2, base + 10, base + 20, base + 30, base + 100}
    assert result == expected
