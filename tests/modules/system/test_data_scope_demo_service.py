"""DataScopeDemoService.list 数据权限端到端测试。

这是数据权限演示功能的核心测试：验证 5 种 data_scope 经过 service 层后
正确地缩减可见数据集。回归保护——任何对 app/utils/data_scope.py 的
改动如果改变了 filter 形态，这里应立即失败。

测试用 db_session fixture（SAVEPOINT 回滚），自清残留。
"""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.db.base import role_depts, user_depts, user_roles
from app.modules.system.models.data_scope_demo import DataScopeDemo
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.data_scope_demo import (
    DataScopeDemoCreate,
    DataScopeDemoQuery,
)
from app.modules.system.service.data_scope_demo_service import (
    data_scope_demo_service,
)

# ---------------------------------------------------------------------------
# 数据准备 helpers
# ---------------------------------------------------------------------------

DEMO_PASSWORD_HASH = "$2b$12$dummyhash"  # 测试不验证密码，hash 占位即可


def _make_dept(*, dept_id: int, name: str, ancestors: str = "0", status="1") -> Dept:
    return Dept(
        dept_id=dept_id,
        dept_name=name,
        ancestors=ancestors,
        order_num=0,
        status=status,
    )


def _make_role(
    *, role_id: int, role_code: str, data_scope: str, status: str = STATUS_ENABLED
) -> Role:
    return Role(
        role_id=role_id,
        role_name=role_code,
        role_code=role_code,
        data_scope=data_scope,
        status=status,
    )


async def _add_user(
    db: AsyncSession,
    *,
    user_id: int,
    user_name: str,
    role_ids: list[int],
    dept_ids: list[int],
    primary_dept_id: int | None = None,
) -> User:
    """建用户并挂角色 + 部门（用关联表直接 insert 绕过 ORM 缓存）。"""
    user = User(
        user_id=user_id,
        user_name=user_name,
        nickname=user_name,
        hashed_password=DEMO_PASSWORD_HASH,
        status=STATUS_ENABLED,
    )
    db.add(user)
    await db.flush()

    if role_ids:
        await db.execute(
            insert(user_roles).values(
                [{"user_id": user_id, "role_id": rid} for rid in role_ids]
            )
        )
    if dept_ids:
        primary = primary_dept_id or dept_ids[0]
        await db.execute(
            insert(user_depts).values(
                [
                    {
                        "user_id": user_id,
                        "dept_id": did,
                        "is_primary": "Y" if did == primary else "N",
                    }
                    for did in dept_ids
                ]
            )
        )
    # 重新查以加载 roles/depts 关系（lazy="selectin" 会自动加载）
    result = await db.execute(select(User).where(User.user_id == user_id))
    return result.scalars().first()


async def _add_demo(
    db: AsyncSession,
    *,
    demo_id: int,
    title: str,
    dept_id: int,
    create_by: int,
) -> DataScopeDemo:
    """直接用 ORM 加 demo 数据。"""
    demo = DataScopeDemo(
        demo_id=demo_id,
        title=title,
        dept_id=dept_id,
        create_by=create_by,
        status=STATUS_ENABLED,
    )
    db.add(demo)
    return demo


async def _visible_ids(db: AsyncSession, current_user: User) -> set[int]:
    """跑 service.get_list 并提取可见 demo_id 集合。"""
    query = DataScopeDemoQuery(current=1, size=100)
    page = await data_scope_demo_service.get_list(db, query, current_user)
    return {d.demo_id for d in page.records}


# ---------------------------------------------------------------------------
# 部门树 fixture：3 层结构
#
#   D_ROOT (1001)
#   ├── D_BRANCH_A (1002)   ancestors = "0,1001"
#   │   ├── D_TEAM_A1 (1003)  ancestors = "0,1001,1002"
#   │   └── D_TEAM_A2 (1004)  ancestors = "0,1001,1002"
#   └── D_BRANCH_B (1005)   ancestors = "0,1001"
#       └── D_TEAM_B1 (1006)  ancestors = "0,1001,1005"
# ---------------------------------------------------------------------------

ROOT_ID = 1001
BRANCH_A_ID = 1002
TEAM_A1_ID = 1003
TEAM_A2_ID = 1004
BRANCH_B_ID = 1005
TEAM_B1_ID = 1006


@pytest.fixture
async def dept_tree(db_session: AsyncSession) -> None:
    """3 层部门树。每个测试独立回滚，不会互相污染。"""
    db_session.add_all(
        [
            _make_dept(dept_id=ROOT_ID, name="总公司"),
            _make_dept(
                dept_id=BRANCH_A_ID, name="华东分公司", ancestors=f"0,{ROOT_ID}"
            ),
            _make_dept(
                dept_id=TEAM_A1_ID,
                name="华东-销售组",
                ancestors=f"0,{ROOT_ID},{BRANCH_A_ID}",
            ),
            _make_dept(
                dept_id=TEAM_A2_ID,
                name="华东-客服组",
                ancestors=f"0,{ROOT_ID},{BRANCH_A_ID}",
            ),
            _make_dept(
                dept_id=BRANCH_B_ID, name="华南分公司", ancestors=f"0,{ROOT_ID}"
            ),
            _make_dept(
                dept_id=TEAM_B1_ID,
                name="华南-销售组",
                ancestors=f"0,{ROOT_ID},{BRANCH_B_ID}",
            ),
        ]
    )
    await db_session.flush()


# ---------------------------------------------------------------------------
# Role fixture：5 种 scope 各一个角色
# ---------------------------------------------------------------------------

ROLE_ALL_ID = 5001
ROLE_CUSTOM_ID = 5002
ROLE_DEPT_ID = 5003
ROLE_DEPT_SUB_ID = 5004
ROLE_SELF_ID = 5005

# dept_tree / roles 是 setup fixture，所有测试都依赖，但代码不直接引用
# 它们的返回值（只用 ROLE_*_ID 常量）。用 usefixtures 自动注入，避免
# 在每个测试签名里显式声明导致 ARG002 误报。
pytestmark = pytest.mark.usefixtures("dept_tree", "roles")


@pytest.fixture
async def roles(db_session: AsyncSession) -> None:
    # role_code 用 R_TEST_* 前缀避免与 seed_demo_data_scope.py 的 R_DEMO_* 冲突
    db_session.add_all(
        [
            _make_role(
                role_id=ROLE_ALL_ID,
                role_code="R_TEST_DEMO_ALL",
                data_scope=DATA_SCOPE_ALL,
            ),
            _make_role(
                role_id=ROLE_CUSTOM_ID,
                role_code="R_TEST_DEMO_CUSTOM",
                data_scope=DATA_SCOPE_CUSTOM,
            ),
            _make_role(
                role_id=ROLE_DEPT_ID,
                role_code="R_TEST_DEMO_DEPT",
                data_scope=DATA_SCOPE_DEPT,
            ),
            _make_role(
                role_id=ROLE_DEPT_SUB_ID,
                role_code="R_TEST_DEMO_DEPT_SUB",
                data_scope=DATA_SCOPE_DEPT_AND_SUB,
            ),
            _make_role(
                role_id=ROLE_SELF_ID,
                role_code="R_TEST_DEMO_SELF",
                data_scope=DATA_SCOPE_SELF,
            ),
        ]
    )
    await db_session.flush()


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestListWithSuperAdmin:
    async def test_super_admin_sees_all(self, db_session: AsyncSession):
        """is_super_admin 通过 user_name='admin' 短路返回全部数据。

        构造内存 User（不 db.add 避免唯一约束），data_scope 内部
        is_super_admin 只读 user_name 属性，不需要 user 在 DB 里。
        这样测试在干净 CI 库（无 init_db.py 种子）也能跑。
        """
        admin = User(
            user_id=9999,
            user_name="admin",  # 触发 is_super_admin 短路
            nickname="admin",
            hashed_password=DEMO_PASSWORD_HASH,
            status=STATUS_ENABLED,
        )

        for i in range(5):
            await _add_demo(
                db_session,
                demo_id=8000 + i,
                title=f"T{i}",
                dept_id=TEAM_A1_ID,
                create_by=9001,
            )
        await db_session.flush()

        ids = await _visible_ids(db_session, admin)
        # admin 短路 = 看全部，库内其他 seed 数据也会一并看到。
        # 只断言我创建的 5 条都可见（issubset），不断言精确数量。
        my_ids = {8000, 8001, 8002, 8003, 8004}
        assert my_ids.issubset(ids), (
            f"admin 应看到所有数据（包括本测试创建的），实际缺：{my_ids - ids}"
        )


class TestListWithSelfScope:
    async def test_sees_only_own_created(self, db_session: AsyncSession):
        """SELF scope：只看 create_by == 自己 user_id 的数据。"""
        me = await _add_user(
            db_session,
            user_id=9002,
            user_name="test_demo_self",
            role_ids=[ROLE_SELF_ID],
            dept_ids=[TEAM_A1_ID],
        )
        # 9002 自己创建 2 条
        await _add_demo(
            db_session, demo_id=8101, title="mine-1", dept_id=TEAM_A1_ID, create_by=9002
        )
        await _add_demo(
            db_session, demo_id=8102, title="mine-2", dept_id=TEAM_A2_ID, create_by=9002
        )
        # 别人创建的，不应可见
        await _add_demo(
            db_session,
            demo_id=8103,
            title="other",
            dept_id=TEAM_A1_ID,
            create_by=9999,
        )
        await db_session.flush()

        ids = await _visible_ids(db_session, me)
        assert ids == {8101, 8102}, f"SELF scope 只应看到自己创建的，实际 = {ids}"


class TestListWithDeptScope:
    async def test_sees_only_own_dept(self, db_session: AsyncSession):
        """DEPT scope：只看 user_depts 关联的部门数据（不含子部门）。"""
        me = await _add_user(
            db_session,
            user_id=9003,
            user_name="test_demo_dept",
            role_ids=[ROLE_DEPT_ID],
            dept_ids=[BRANCH_A_ID],  # 挂在华东分公司（不含子组）
        )
        # BRANCH_A 下的数据
        await _add_demo(
            db_session,
            demo_id=8201,
            title="branch-a",
            dept_id=BRANCH_A_ID,
            create_by=9003,
        )
        # TEAM_A1（华东子组）的数据，DEPT scope 不应可见
        await _add_demo(
            db_session,
            demo_id=8202,
            title="team-a1",
            dept_id=TEAM_A1_ID,
            create_by=9003,
        )
        # TEAM_A2 同理
        await _add_demo(
            db_session,
            demo_id=8203,
            title="team-a2",
            dept_id=TEAM_A2_ID,
            create_by=9003,
        )
        # BRANCH_B 的数据，绝不可见
        await _add_demo(
            db_session,
            demo_id=8204,
            title="branch-b",
            dept_id=BRANCH_B_ID,
            create_by=9003,
        )
        await db_session.flush()

        ids = await _visible_ids(db_session, me)
        assert ids == {8201}, f"DEPT scope 只看本部门不含子，实际 = {ids}"


class TestListWithDeptAndSubScope:
    async def test_sees_own_dept_and_subtree(self, db_session: AsyncSession):
        """DEPT_AND_SUB：本部门 + 所有子部门。"""
        me = await _add_user(
            db_session,
            user_id=9004,
            user_name="test_demo_dept_sub",
            role_ids=[ROLE_DEPT_SUB_ID],
            dept_ids=[BRANCH_A_ID],  # 挂华东分公司
        )
        await _add_demo(
            db_session,
            demo_id=8301,
            title="branch-a",
            dept_id=BRANCH_A_ID,
            create_by=9004,
        )
        await _add_demo(
            db_session,
            demo_id=8302,
            title="team-a1",
            dept_id=TEAM_A1_ID,
            create_by=9004,
        )
        await _add_demo(
            db_session,
            demo_id=8303,
            title="team-a2",
            dept_id=TEAM_A2_ID,
            create_by=9004,
        )
        # 华南的不可见
        await _add_demo(
            db_session,
            demo_id=8304,
            title="branch-b",
            dept_id=BRANCH_B_ID,
            create_by=9004,
        )
        await _add_demo(
            db_session,
            demo_id=8305,
            title="team-b1",
            dept_id=TEAM_B1_ID,
            create_by=9004,
        )
        await db_session.flush()

        ids = await _visible_ids(db_session, me)
        assert ids == {8301, 8302, 8303}, f"DEPT_AND_SUB 应含子树，实际 = {ids}"


class TestListWithCustomScope:
    async def test_sees_only_configured_depts(self, db_session: AsyncSession):
        """CUSTOM：只看 role_depts 配置的部门（与 user 自己所在部门无关）。"""
        # role 配置可见 TEAM_A1 + TEAM_B1（跨分公司）
        await db_session.execute(
            insert(role_depts).values(
                [
                    {"role_id": ROLE_CUSTOM_ID, "dept_id": TEAM_A1_ID},
                    {"role_id": ROLE_CUSTOM_ID, "dept_id": TEAM_B1_ID},
                ]
            )
        )
        # me 自己挂在 BRANCH_A，但 CUSTOM 不看 user_depts
        me = await _add_user(
            db_session,
            user_id=9005,
            user_name="test_demo_custom",
            role_ids=[ROLE_CUSTOM_ID],
            dept_ids=[BRANCH_A_ID],
        )
        for i, dept_id in enumerate([TEAM_A1_ID, TEAM_A2_ID, TEAM_B1_ID, BRANCH_B_ID]):
            await _add_demo(
                db_session,
                demo_id=8401 + i,
                title=f"d{i}",
                dept_id=dept_id,
                create_by=9005,
            )
        await db_session.flush()

        ids = await _visible_ids(db_session, me)
        assert ids == {8401, 8403}, (
            f"CUSTOM 只看 role_depts 配置的 TEAM_A1/TEAM_B1，实际 = {ids}"
        )

    async def test_excludes_disabled_dept(self, db_session: AsyncSession):
        """CUSTOM 排除被禁用的部门（admin 禁用部门 = 撤销 CUSTOM 授权）。"""
        # 新增一个禁用的部门，role 关联它
        disabled_dept_id = 9100
        db_session.add(
            _make_dept(
                dept_id=disabled_dept_id,
                name="已禁用部门",
                ancestors=f"0,{ROOT_ID}",
                status=STATUS_DISABLED,
            )
        )
        await db_session.flush()

        await db_session.execute(
            insert(role_depts).values(
                [
                    {"role_id": ROLE_CUSTOM_ID, "dept_id": TEAM_A1_ID},
                    {"role_id": ROLE_CUSTOM_ID, "dept_id": disabled_dept_id},
                ]
            )
        )
        me = await _add_user(
            db_session,
            user_id=9006,
            user_name="test_demo_custom_disabled",
            role_ids=[ROLE_CUSTOM_ID],
            dept_ids=[TEAM_A1_ID],
        )
        await _add_demo(
            db_session,
            demo_id=8501,
            title="enabled-dept-data",
            dept_id=TEAM_A1_ID,
            create_by=9006,
        )
        await _add_demo(
            db_session,
            demo_id=8502,
            title="disabled-dept-data",
            dept_id=disabled_dept_id,
            create_by=9006,
        )
        await db_session.flush()

        ids = await _visible_ids(db_session, me)
        assert ids == {8501}, f"CUSTOM 应排除禁用部门的数据，实际 = {ids}"


class TestListWithAllScope:
    async def test_sees_everything(self, db_session: AsyncSession):
        """ALL scope：不过滤，看到全部。"""
        me = await _add_user(
            db_session,
            user_id=9007,
            user_name="test_demo_all",
            role_ids=[ROLE_ALL_ID],
            dept_ids=[TEAM_A1_ID],
        )
        for i, dept_id in enumerate(
            [ROOT_ID, BRANCH_A_ID, TEAM_A1_ID, TEAM_A2_ID, BRANCH_B_ID, TEAM_B1_ID]
        ):
            await _add_demo(
                db_session,
                demo_id=8601 + i,
                title=f"d{i}",
                dept_id=dept_id,
                create_by=9007,
            )
        await db_session.flush()

        ids = await _visible_ids(db_session, me)
        # ALL scope 不过滤，会看到库内所有数据（包括 seed 演示数据）。
        # 只断言我创建的 6 条都可见，不断言精确总数。
        my_ids = {8601, 8602, 8603, 8604, 8605, 8606}
        assert my_ids.issubset(ids), (
            f"ALL scope 应看到所有数据（包括本测试创建的），实际缺：{my_ids - ids}"
        )


class TestCreateInjectsCurrentUserContext:
    """create 时 dept_id / create_by 必须从 current_user 注入，前端伪造无效。"""

    async def test_uses_primary_dept_and_user_id(self, db_session: AsyncSession):
        me = await _add_user(
            db_session,
            user_id=9008,
            user_name="test_demo_creator",
            role_ids=[ROLE_SELF_ID],
            dept_ids=[TEAM_A1_ID, TEAM_A2_ID],  # 多部门，主部门是 TEAM_A1
            primary_dept_id=TEAM_A1_ID,
        )

        # 即使前端传 dept_id/create_by，service 也应忽略
        create_in = DataScopeDemoCreate(
            title="new",
            content="x",
            status=STATUS_ENABLED,
        )
        # Pydantic 默认禁止额外字段；如果 schema 设计正确，title/content/status 之外的字段无法传
        demo = await data_scope_demo_service.create(db_session, create_in, me)
        await db_session.flush()

        assert demo.create_by == 9008
        assert demo.dept_id == TEAM_A1_ID, "create 应使用 current_user 的主部门"
