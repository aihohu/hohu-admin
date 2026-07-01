"""role_service 数据权限维护测试。

回归一：update_role 改 data_scope 从 CUSTOM → 其他时，role.depts 关联
必须清空。旧实现只在显式传 dept_ids 时才走清理分支，前端只发
{"dataScope": "3"} 的请求会跳过清理，导致 role_depts 表残留过期关联，
下次改回 CUSTOM 时旧 depts 复活，与 UI 显示不一致。

回归二：create_role / update_role 收到不存在的 dept_ids 时必须抛错。
旧实现用 select(...).where(dept_id.in_(ids))，不存在的 ID 被静默丢弃，
管理员无法发现配置漂移（误以为配了 5 个部门，实际只 3 个生效）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    STATUS_ENABLED,
)
from app.core.exceptions import InvalidParameterException
from app.db.base import role_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
from app.modules.system.service.role_service import role_service


def _make_dept(*, dept_id: int, name: str) -> Dept:
    return Dept(
        dept_id=dept_id,
        dept_name=name,
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )


async def _role_dept_ids(db: AsyncSession, role_id: int) -> set[int]:
    """直接查 role_depts 关联表，绕开 ORM 缓存。"""
    result = await db.execute(
        select(role_depts.c.dept_id).where(role_depts.c.role_id == role_id)
    )
    return set(result.scalars().all())


class TestUpdateRoleClearsDeptsOnScopeChange:
    """Bug 1: data_scope 离开 CUSTOM 时必须清空 role_depts。"""

    async def test_clears_depts_when_scope_changes_from_custom_to_dept(
        self, db_session: AsyncSession
    ):
        """CUSTOM→DEPT：只传 dataScope，不传 dept_ids，role_depts 应清空。"""
        dept_a = _make_dept(dept_id=900000001, name="D-A")
        dept_b = _make_dept(dept_id=900000002, name="D-B")
        db_session.add_all([dept_a, dept_b])
        await db_session.flush()

        role = Role(
            role_name="R-CUSTOM",
            role_code="R_TEST_CLEAR_1",
            data_scope=DATA_SCOPE_CUSTOM,
            status=STATUS_ENABLED,
        )
        role.depts = [dept_a, dept_b]
        db_session.add(role)
        await db_session.flush()

        # 关键回归操作：只改 data_scope，前端不传 dept_ids
        update = RoleUpdate(data_scope=DATA_SCOPE_DEPT)
        await role_service.update_role(db_session, role.role_id, update)

        # 强制 flush 让 role_depts 写库，再查关联表
        await db_session.flush()
        assert await _role_dept_ids(db_session, role.role_id) == set(), (
            "data_scope 从 CUSTOM 改为其他时，role_depts 必须清空"
        )

    async def test_clears_depts_when_scope_changes_from_custom_to_all(
        self, db_session: AsyncSession
    ):
        """CUSTOM→ALL：role_depts 应清空。"""
        dept = _make_dept(dept_id=900000003, name="D-C")
        db_session.add(dept)
        await db_session.flush()

        role = Role(
            role_name="R-CUSTOM-2",
            role_code="R_TEST_CLEAR_2",
            data_scope=DATA_SCOPE_CUSTOM,
            status=STATUS_ENABLED,
        )
        role.depts = [dept]
        db_session.add(role)
        await db_session.flush()

        update = RoleUpdate(data_scope=DATA_SCOPE_ALL)
        await role_service.update_role(db_session, role.role_id, update)
        await db_session.flush()

        assert await _role_dept_ids(db_session, role.role_id) == set()


class TestCreateRoleValidatesDeptIds:
    """Bug 2: create_role 收到不存在的 dept_ids 必须抛 InvalidParameterException。"""

    async def test_raises_when_dept_id_does_not_exist(self, db_session: AsyncSession):
        """dept_ids 含不存在 ID 时拒绝创建。"""
        dept = _make_dept(dept_id=900000010, name="D-EXISTS")
        db_session.add(dept)
        await db_session.flush()

        create = RoleCreate(
            role_name="R-NEW",
            role_code="R_TEST_CREATE_1",
            data_scope=DATA_SCOPE_CUSTOM,
            status=STATUS_ENABLED,
            dept_ids=[900000010, 999999999],  # 第二个不存在
        )

        try:
            await role_service.create_role(db_session, create)
        except InvalidParameterException as e:
            assert "999999999" in str(e) or "部门" in str(e)
            return
        raise AssertionError("应抛 InvalidParameterException")

    async def test_succeeds_when_all_dept_ids_exist(self, db_session: AsyncSession):
        """全部 dept_ids 都存在时正常创建。"""
        dept_a = _make_dept(dept_id=900000011, name="D-A")
        dept_b = _make_dept(dept_id=900000012, name="D-B")
        db_session.add_all([dept_a, dept_b])
        await db_session.flush()

        create = RoleCreate(
            role_name="R-OK",
            role_code="R_TEST_CREATE_2",
            data_scope=DATA_SCOPE_CUSTOM,
            status=STATUS_ENABLED,
            dept_ids=[900000011, 900000012],
        )

        role = await role_service.create_role(db_session, create)
        await db_session.flush()
        assert await _role_dept_ids(db_session, role.role_id) == {
            900000011,
            900000012,
        }


class TestUpdateRoleValidatesDeptIds:
    """Bug 2: update_role 收到不存在的 dept_ids 必须抛 InvalidParameterException。"""

    async def test_raises_when_dept_id_does_not_exist(self, db_session: AsyncSession):
        """update_role 时 dept_ids 含不存在 ID 拒绝更新。"""
        dept = _make_dept(dept_id=900000020, name="D-EXISTS")
        db_session.add(dept)
        await db_session.flush()

        role = Role(
            role_name="R-ORIG",
            role_code="R_TEST_UPDATE_1",
            data_scope=DATA_SCOPE_CUSTOM,
            status=STATUS_ENABLED,
        )
        db_session.add(role)
        await db_session.flush()
        # 模拟生产环境：让 db.get 触发 fresh SELECT + selectin(depts)，
        # 否则 identity map 缓存中 depts 未加载，role.depts = [...] 会触发
        # MissingGreenlet。
        db_session.expunge(role)
        await db_session.flush()

        update = RoleUpdate(
            data_scope=DATA_SCOPE_CUSTOM,
            dept_ids=[900000020, 888888888],  # 第二个不存在
        )

        try:
            await role_service.update_role(db_session, role.role_id, update)
        except InvalidParameterException:
            return
        raise AssertionError("应抛 InvalidParameterException")
