"""data_scope 过滤器端到端测试。

覆盖：
- _get_best_scope：多角色取最大权限，禁用角色不参与。
- get_user_data_scope_filters：5 种 scope 各自的过滤器形态 + fallback。
- get_data_scope_filters：通用模型（带 dept_id 字段）的过滤。

回归覆盖：以前只测了 _get_dept_and_sub_ids + is_super_admin，过滤主路径
（_get_best_scope / get_user_data_scope_filters / _get_custom_dept_ids）
没有任何测试保护，重构容易引入回归。
"""

from sqlalchemy import String, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.db.base import Base, role_depts, user_depts, user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.utils.data_scope import (
    _get_best_scope,
    get_data_scope_filters,
    get_user_data_scope_filters,
)
from tests.tenant_helpers import tenant_context

TENANT = tenant_context()


def _make_user(*, user_id: int, user_name: str, roles=None, depts=None) -> User:
    """内存构造 User，绕开 hashed_password 等必填约束。"""
    user = User(
        tenant_id=TENANT.tenant_id,
        user_id=user_id,
        user_name=user_name,
        status=STATUS_ENABLED,
        hashed_password="x",
    )
    user.roles = roles or []
    user.depts = depts or []
    return user


def _make_role(
    *, role_id: int, role_code: str, data_scope: str, status: str = STATUS_ENABLED
) -> Role:
    return Role(
        tenant_id=TENANT.tenant_id,
        role_id=role_id,
        role_name=role_code,
        role_code=role_code,
        data_scope=data_scope,
        status=status,
    )


def _make_dept(*, dept_id: int, name: str, ancestors: str = "0") -> Dept:
    return Dept(
        tenant_id=TENANT.tenant_id,
        dept_id=dept_id,
        dept_name=name,
        ancestors=ancestors,
        order_num=0,
        status=STATUS_ENABLED,
    )


# ---------------------------------------------------------------------------
# _get_best_scope：纯函数测试
# ---------------------------------------------------------------------------


class TestGetBestScope:
    def test_picks_max_priority_across_roles(self):
        """ALL(5) > DEPT_AND_SUB(4) > DEPT(3) > CUSTOM(2) > SELF(1)。"""
        user = _make_user(
            user_id=1,
            user_name="u",
            roles=[
                _make_role(role_id=1, role_code="R_SELF", data_scope=DATA_SCOPE_SELF),
                _make_role(role_id=2, role_code="R_DEPT", data_scope=DATA_SCOPE_DEPT),
                _make_role(
                    role_id=3, role_code="R_CUSTOM", data_scope=DATA_SCOPE_CUSTOM
                ),
            ],
        )
        assert _get_best_scope(user) == DATA_SCOPE_DEPT

    def test_all_beats_everything(self):
        user = _make_user(
            user_id=1,
            user_name="u",
            roles=[
                _make_role(role_id=1, role_code="R_SELF", data_scope=DATA_SCOPE_SELF),
                _make_role(role_id=2, role_code="R_ALL", data_scope=DATA_SCOPE_ALL),
            ],
        )
        assert _get_best_scope(user) == DATA_SCOPE_ALL

    def test_disabled_role_does_not_contribute(self):
        """status != 1 的角色不参与最大值计算。"""
        user = _make_user(
            user_id=1,
            user_name="u",
            roles=[
                _make_role(
                    role_id=1,
                    role_code="R_DISABLED_ALL",
                    data_scope=DATA_SCOPE_ALL,
                    status=STATUS_DISABLED,
                ),
                _make_role(role_id=2, role_code="R_SELF", data_scope=DATA_SCOPE_SELF),
            ],
        )
        # 禁用的 ALL 不算，只剩 SELF
        assert _get_best_scope(user) == DATA_SCOPE_SELF

    def test_no_enabled_roles_falls_back_to_self(self):
        user = _make_user(user_id=1, user_name="u", roles=[])
        assert _get_best_scope(user) == DATA_SCOPE_SELF


# ---------------------------------------------------------------------------
# get_user_data_scope_filters：DB 端到端测试
# ---------------------------------------------------------------------------


class TestGetUserDataScopeFilters:
    """验证各 scope 返回的过滤器形态以及应用到 select(User) 后的结果。"""

    async def _setup_role(
        self,
        db: AsyncSession,
        *,
        role_id: int,
        data_scope: str,
        role_dept_ids: list[int] | None = None,
    ) -> Role:
        role = _make_role(
            role_id=role_id, role_code=f"R_{role_id}", data_scope=data_scope
        )
        db.add(role)
        await db.flush()
        if role_dept_ids:
            await db.execute(
                insert(role_depts).values(
                    [
                        {
                            "tenant_id": TENANT.tenant_id,
                            "role_id": role_id,
                            "dept_id": did,
                        }
                        for did in role_dept_ids
                    ]
                )
            )
        return role

    async def _setup_user(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        user_name: str,
        role_ids: list[int],
        dept_ids: list[int],
    ) -> User:
        user = User(
            tenant_id=TENANT.tenant_id,
            user_id=user_id,
            user_name=user_name,
            hashed_password="x",
            status=STATUS_ENABLED,
        )
        db.add(user)
        await db.flush()
        if role_ids:
            await db.execute(
                insert(user_roles).values(
                    [
                        {
                            "tenant_id": TENANT.tenant_id,
                            "user_id": user_id,
                            "role_id": rid,
                        }
                        for rid in role_ids
                    ]
                )
            )
        if dept_ids:
            await db.execute(
                insert(user_depts).values(
                    [
                        {
                            "tenant_id": TENANT.tenant_id,
                            "user_id": user_id,
                            "dept_id": did,
                            "is_primary": "N",
                        }
                        for did in dept_ids
                    ]
                )
            )
        # 重新查以加载关系
        result = await db.execute(
            select(User).where(
                User.tenant_id == TENANT.tenant_id,
                User.user_id == user_id,
            )
        )
        return result.scalars().first()

    async def test_self_scope_returns_only_self(self, db_session: AsyncSession):
        """DATA_SCOPE_SELF：返回过滤器只匹配自己。"""
        user = await self._setup_user(
            db_session,
            user_id=1001,
            user_name="self_user",
            role_ids=[],
            dept_ids=[],
        )
        user.roles = []  # 无角色 → _get_best_scope 返回 SELF
        filters = await get_user_data_scope_filters(db_session, user, tenant=TENANT)
        assert len(filters) == 2
        # 应用过滤器，只返回 user 自己
        result = await db_session.execute(select(User).where(*filters))
        ids = {u.user_id for u in result.scalars().all()}
        assert ids == {1001}

    async def test_dept_scope_with_user_depts(self, db_session: AsyncSession):
        """DATA_SCOPE_DEPT：返回本部门用户。"""
        # 建部门、角色、用户
        base = 920000000
        for i in range(1, 4):
            db_session.add(_make_dept(dept_id=base + i, name=f"D{i}"))
        await db_session.flush()

        await self._setup_role(db_session, role_id=2001, data_scope=DATA_SCOPE_DEPT)
        # self_user 在 D1，同事 coworker 也在 D1，陌生人 stranger 在 D2
        self_user = await self._setup_user(
            db_session,
            user_id=1001,
            user_name="self",
            role_ids=[2001],
            dept_ids=[base + 1],
        )
        await self._setup_user(
            db_session,
            user_id=1002,
            user_name="coworker",
            role_ids=[],
            dept_ids=[base + 1],
        )
        await self._setup_user(
            db_session,
            user_id=1003,
            user_name="stranger",
            role_ids=[],
            dept_ids=[base + 2],
        )

        filters = await get_user_data_scope_filters(
            db_session, self_user, tenant=TENANT
        )
        result = await db_session.execute(select(User).where(*filters))
        ids = {u.user_id for u in result.scalars().all()}
        assert ids == {1001, 1002}, f"DEPT 应返回本部门所有用户，实际 = {ids}"

    async def test_dept_scope_without_depts_falls_back_to_self(
        self, db_session: AsyncSession
    ):
        """DATA_SCOPE_DEPT 但用户未挂任何部门 → fallback 到 self。"""
        await self._setup_role(db_session, role_id=2002, data_scope=DATA_SCOPE_DEPT)
        self_user = await self._setup_user(
            db_session, user_id=1001, user_name="self", role_ids=[2002], dept_ids=[]
        )

        filters = await get_user_data_scope_filters(
            db_session, self_user, tenant=TENANT
        )
        assert len(filters) == 2
        result = await db_session.execute(select(User).where(*filters))
        ids = {u.user_id for u in result.scalars().all()}
        assert ids == {1001}

    async def test_custom_scope_with_role_depts(self, db_session: AsyncSession):
        """DATA_SCOPE_CUSTOM：按 role_depts 配置的部门过滤。"""
        base = 920100000
        for i in range(1, 4):
            db_session.add(_make_dept(dept_id=base + i, name=f"D{i}"))
        await db_session.flush()

        # role 配置可以看 D1, D2（不是用户自己所在部门）
        await self._setup_role(
            db_session,
            role_id=2003,
            data_scope=DATA_SCOPE_CUSTOM,
            role_dept_ids=[base + 1, base + 2],
        )
        self_user = await self._setup_user(
            db_session,
            user_id=1001,
            user_name="self",
            role_ids=[2003],
            dept_ids=[base + 3],
        )
        # D1 有 coworker，D2 有 other，D3 有 self（但 role 不允许看 D3）
        await self._setup_user(
            db_session,
            user_id=1002,
            user_name="coworker",
            role_ids=[],
            dept_ids=[base + 1],
        )
        await self._setup_user(
            db_session,
            user_id=1003,
            user_name="other",
            role_ids=[],
            dept_ids=[base + 2],
        )
        await self._setup_user(
            db_session,
            user_id=1004,
            user_name="d3_user",
            role_ids=[],
            dept_ids=[base + 3],
        )

        filters = await get_user_data_scope_filters(
            db_session, self_user, tenant=TENANT
        )
        result = await db_session.execute(select(User).where(*filters))
        ids = {u.user_id for u in result.scalars().all()}
        # 只看 D1, D2 里的用户（含自己也只有当自己在 D1/D2；这里 self 在 D3 不返回）
        assert ids == {1002, 1003}, f"CUSTOM 应按 role_depts 过滤，实际 = {ids}"

    async def test_custom_scope_without_role_depts_falls_back_to_self(
        self, db_session: AsyncSession
    ):
        """DATA_SCOPE_CUSTOM 但 role 未配任何 depts → fallback 到 self。"""
        await self._setup_role(
            db_session, role_id=2004, data_scope=DATA_SCOPE_CUSTOM, role_dept_ids=[]
        )
        self_user = await self._setup_user(
            db_session, user_id=1001, user_name="self", role_ids=[2004], dept_ids=[]
        )

        filters = await get_user_data_scope_filters(
            db_session, self_user, tenant=TENANT
        )
        assert len(filters) == 2
        result = await db_session.execute(select(User).where(*filters))
        ids = {u.user_id for u in result.scalars().all()}
        assert ids == {1001}

    async def test_custom_scope_excludes_disabled_depts(self, db_session: AsyncSession):
        """DATA_SCOPE_CUSTOM 不应包含被禁用的部门。

        场景：admin 配了 role 看部门 A、B，后来把 B 禁用。本次访问应只
        返回 A 部门用户，B 部门用户不可见（admin 禁用 = 撤销 CUSTOM 授权）。
        """
        base = 940000000
        enabled_dept = Dept(
            tenant_id=TENANT.tenant_id,
            dept_id=base + 1,
            dept_name="A-enabled",
            ancestors="0",
            order_num=0,
            status=STATUS_ENABLED,
        )
        disabled_dept = Dept(
            tenant_id=TENANT.tenant_id,
            dept_id=base + 2,
            dept_name="B-disabled",
            ancestors="0",
            order_num=0,
            status=STATUS_DISABLED,
        )
        db_session.add_all([enabled_dept, disabled_dept])
        await db_session.flush()

        # role 关联两个 dept（含禁用）
        await self._setup_role(
            db_session,
            role_id=2006,
            data_scope=DATA_SCOPE_CUSTOM,
            role_dept_ids=[base + 1, base + 2],
        )
        # self_user 必须挂某个部门，避免 fallback 路径
        self_user = await self._setup_user(
            db_session,
            user_id=1001,
            user_name="self",
            role_ids=[2006],
            dept_ids=[base + 1],
        )
        await self._setup_user(
            db_session,
            user_id=1002,
            user_name="a_user",
            role_ids=[],
            dept_ids=[base + 1],
        )
        await self._setup_user(
            db_session,
            user_id=1003,
            user_name="b_user",
            role_ids=[],
            dept_ids=[base + 2],
        )

        filters = await get_user_data_scope_filters(
            db_session, self_user, tenant=TENANT
        )
        result = await db_session.execute(select(User).where(*filters))
        ids = {u.user_id for u in result.scalars().all()}
        # 只应看到 A 部门用户，B 部门用户因 dept 被禁用而不可见
        assert ids == {1001, 1002}, f"CUSTOM 应排除禁用部门，实际 = {ids}"
        assert 1003 not in ids

    async def test_dept_and_sub_scope_includes_descendants(
        self, db_session: AsyncSession
    ):
        """DATA_SCOPE_DEPT_AND_SUB：返回本部门及所有子部门用户。"""
        base = 920200000
        # D1 是顶层，D2 在 D1 下，D3 在 D2 下
        db_session.add_all(
            [
                _make_dept(dept_id=base + 1, name="D1", ancestors="0"),
                _make_dept(dept_id=base + 2, name="D2", ancestors=f"0,{base + 1}"),
                _make_dept(
                    dept_id=base + 3, name="D3", ancestors=f"0,{base + 1},{base + 2}"
                ),
                # 干扰项：D4 顶层，名字相似但不在 D1 子树
                _make_dept(dept_id=base + 10, name="D10", ancestors="0"),
            ]
        )
        await db_session.flush()

        await self._setup_role(
            db_session, role_id=2005, data_scope=DATA_SCOPE_DEPT_AND_SUB
        )
        self_user = await self._setup_user(
            db_session,
            user_id=1001,
            user_name="self",
            role_ids=[2005],
            dept_ids=[base + 1],
        )
        await self._setup_user(
            db_session,
            user_id=1002,
            user_name="d2_user",
            role_ids=[],
            dept_ids=[base + 2],
        )
        await self._setup_user(
            db_session,
            user_id=1003,
            user_name="d3_user",
            role_ids=[],
            dept_ids=[base + 3],
        )
        await self._setup_user(
            db_session,
            user_id=1004,
            user_name="d10_user",
            role_ids=[],
            dept_ids=[base + 10],
        )

        filters = await get_user_data_scope_filters(
            db_session, self_user, tenant=TENANT
        )
        result = await db_session.execute(select(User).where(*filters))
        ids = {u.user_id for u in result.scalars().all()}
        # 应包含 D1(self), D2, D3，不含 D10
        assert ids == {1001, 1002, 1003}, f"DEPT_AND_SUB 应含子树，实际 = {ids}"


# ---------------------------------------------------------------------------
# get_data_scope_filters：通用模型测试
# ---------------------------------------------------------------------------


class _DummyModel(Base):
    """模拟带 dept_id 字段的业务模型。"""

    __tablename__ = "test_dummy_business"

    id: Mapped[int] = mapped_column(primary_key=True)
    dept_id: Mapped[int] = mapped_column(default=0)
    create_by: Mapped[int] = mapped_column(default=0)
    title: Mapped[str] = mapped_column(String(50), default="")


class TestGetDataScopeFilters:
    async def test_all_scope_returns_empty(self, db_session: AsyncSession):
        """DATA_SCOPE_ALL：不过滤。"""
        user = _make_user(
            user_id=1,
            user_name="u",
            roles=[_make_role(role_id=1, role_code="R_ALL", data_scope=DATA_SCOPE_ALL)],
        )
        filters = await get_data_scope_filters(
            db_session, user, _DummyModel, tenant=TENANT
        )
        assert filters == []

    async def test_self_scope_filters_by_create_by(self, db_session: AsyncSession):
        """DATA_SCOPE_SELF：按 create_by 字段过滤。"""
        user = _make_user(
            user_id=42,
            user_name="u",
            roles=[
                _make_role(role_id=1, role_code="R_SELF", data_scope=DATA_SCOPE_SELF)
            ],
        )
        filters = await get_data_scope_filters(
            db_session, user, _DummyModel, tenant=TENANT
        )
        assert len(filters) == 1
        # 应用到 _DummyModel，应只返回 create_by == 42 的记录
        # （这里只验证过滤器形态，不真的建表测试 SQL）
