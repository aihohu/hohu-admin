import pytest
from sqlalchemy import select

from app.modules.marketplace.models import App, AppPermission
from app.modules.marketplace.service.permission_service import permission_service


@pytest.fixture
async def sample_app(db_session):
    """共享样本应用：每个测试拿到一个干净的 App（依赖 db_session 回滚）"""
    app = App(
        tenant_id=0,
        name="测试应用",
        slug="perm-test-app",
        type="lowcode",
        category="business",
        status="published",
    )
    db_session.add(app)
    await db_session.flush()
    return app


class TestPermissionService:
    async def test_bulk_insert_normalizes_detail(self, db_session, sample_app):
        """批量插入权限时自动计算 detail_hash + detail_canonical"""
        permissions = [
            {"type": "api", "detail": {"method": "GET", "path": "/api/v1/users"}},
            {
                "type": "external_api",
                "detail": {
                    "method": "GET",
                    "url": "https://api.weather.com/v1/*",
                },
            },
            {"type": "menu", "detail": {"target": "inject:sidemenu"}},
        ]
        count = await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=permissions
        )
        await db_session.flush()
        assert count == 3

        # 校验 hash + canonical 写入
        result = await db_session.execute(
            select(AppPermission).where(AppPermission.app_id == sample_app.id)
        )
        perms = result.scalars().all()
        assert len(perms) == 3
        for p in perms:
            assert p.detail_hash
            assert len(p.detail_hash) == 64  # SHA-256 hex
            assert p.detail_canonical  # 审计字段非空

    async def test_duplicate_permissions_deduplicated(self, db_session, sample_app):
        """同 (app, type, detail_hash) 唯一约束——重复跳过"""
        perm = {"type": "api", "detail": {"method": "GET", "path": "/x"}}
        count1 = await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=[perm]
        )
        await db_session.flush()
        assert count1 == 1

        # 重新插入同样权限（应用升级），不应该重复
        count2 = await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=[perm, perm]
        )
        await db_session.flush()
        assert count2 == 0  # 全部冲突跳过

        # 校验最终只有 1 条记录
        result = await db_session.execute(
            select(AppPermission).where(AppPermission.app_id == sample_app.id)
        )
        perms = result.scalars().all()
        assert len(perms) == 1

    async def test_bulk_insert_mixed_dedup(self, db_session, sample_app):
        """混合插入：部分新权限 + 部分重复——只插入新权限"""
        perm_a = {"type": "api", "detail": {"method": "GET", "path": "/a"}}
        perm_b = {"type": "api", "detail": {"method": "GET", "path": "/b"}}

        await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=[perm_a]
        )
        await db_session.flush()

        # perm_a 已存在，perm_b 是新权限
        count = await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=[perm_a, perm_b]
        )
        await db_session.flush()
        assert count == 1  # 只有 perm_b 新增

    async def test_bulk_insert_empty_list_returns_zero(self, db_session, sample_app):
        count = await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=[]
        )
        assert count == 0

    async def test_bulk_insert_key_order_independent(self, db_session, sample_app):
        """detail 字典 key 顺序不同，detail_hash 应一致（ON CONFLICT 触发）"""
        # 同一权限，dict 键序不同
        perm1 = {
            "type": "api",
            "detail": {"path": "/x", "method": "GET"},  # path 在前
        }
        perm2 = {
            "type": "api",
            "detail": {"method": "GET", "path": "/x"},  # method 在前
        }
        count1 = await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=[perm1]
        )
        await db_session.flush()
        assert count1 == 1

        count2 = await permission_service.bulk_insert(
            db_session, app_id=sample_app.id, permissions=[perm2]
        )
        await db_session.flush()
        assert count2 == 0  # 同 hash，去重

    async def test_list_by_app_returns_sorted(self, db_session, sample_app):
        """按 type、detail_hash 排序输出"""
        await permission_service.bulk_insert(
            db_session,
            app_id=sample_app.id,
            permissions=[
                {"type": "menu", "detail": {"target": "x"}},
                {"type": "api", "detail": {"method": "GET"}},
                {"type": "api", "detail": {"method": "POST"}},
            ],
        )
        await db_session.flush()

        result = await permission_service.list_by_app(db_session, app_id=sample_app.id)
        assert len(result) == 3
        # 按 type 排序：api 在 menu 前
        assert result[0].type == "api"
        assert result[1].type == "api"
        assert result[2].type == "menu"
        # 同 type 内按 detail_hash 排序（确定性）
        assert result[0].detail_hash <= result[1].detail_hash

    async def test_list_by_app_empty(self, db_session, sample_app):
        """应用无权限声明时返回空列表"""
        result = await permission_service.list_by_app(db_session, app_id=sample_app.id)
        assert result == []
