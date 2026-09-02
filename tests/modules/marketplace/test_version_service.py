import pytest

from app.modules.marketplace.exceptions import (
    AppInvalidManifestException,
    AppNotFoundException,
)
from app.modules.marketplace.models import App
from modules.marketplace import default_version_service as version_service


class TestVersionServiceValidateManifest:
    def test_validate_manifest_minimal_ok(self):
        manifest = {
            "name": "客户管理",
            "slug": "zhangsan-crm",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        }
        version_service.validate_manifest(manifest)  # 不抛即通过

    def test_validate_manifest_invalid_slug_numeric_start(self):
        """slug 必须匹配 ^[a-z][a-z0-9-]{2,148}[a-z0-9]$ —— 数字开头拒绝"""
        manifest = {
            "name": "X",
            "slug": "1numeric-start",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_invalid_slug_trailing_dash(self):
        """末字符不能是连字符"""
        manifest = {
            "name": "X",
            "slug": "trailing-dash-",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_required_field_no_default_rejected(self):
        """spec 6.3：新增 required 字段必须有 default"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"email": {"type": "string"}},  # 无 default
                "required": ["email"],
            },
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "email" in str(exc.value)

    def test_validate_manifest_required_field_with_dynamic_default_rejected(self):
        """spec 决策 #69：default 必须字面常量，禁止 NOW()"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"ts": {"type": "string", "default": "NOW()"}},
                "required": ["ts"],
            },
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_required_field_with_uuid_default_rejected(self):
        """禁止 uuid_generate_v4()"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "default": "uuid_generate_v4()"}
                },
                "required": ["id"],
            },
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_required_field_with_template_default_rejected(self):
        """禁止 {{random_string(8)}}"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "default": "{{random_string(8)}}",
                    }
                },
                "required": ["code"],
            },
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_required_field_with_literal_default_ok(self):
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"level": {"type": "string", "default": "C"}},
                "required": ["level"],
            },
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_required_field_with_numeric_default_ok(self):
        """数值型 default 也应通过"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 0}},
                "required": ["count"],
            },
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_required_field_with_boolean_default_ok(self):
        """布尔型 default 应通过"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"active": {"type": "boolean", "default": True}},
                "required": ["active"],
            },
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_required_field_with_null_default_rejected(self):
        """None 不允许（PG ADD COLUMN NOT NULL 无意义）"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"x": {"type": "string", "default": None}},
                "required": ["x"],
            },
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_invalid_type(self):
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "invalid-type",
            "category": "business",
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "type" in str(exc.value).lower()

    def test_validate_manifest_invalid_category(self):
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "invalid-cat",
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_invalid_version(self):
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "not-semver",
            "type": "lowcode",
            "category": "business",
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    # ---------- permissions 形状校验（spec 14.5）----------

    def test_validate_manifest_permissions_missing_detail_rejected(self):
        """permissions 缺 detail 字段 → 拒绝（之前会触发 KeyError 500）"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": [
                {"type": "api", "path": "/foo"}  # 缺 detail
            ],
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "detail" in str(exc.value)

    def test_validate_manifest_permissions_missing_type_rejected(self):
        """permissions 缺 type 字段 → 拒绝"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": [
                {"detail": {"method": "GET"}}  # 缺 type
            ],
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "type" in str(exc.value)

    def test_validate_manifest_permissions_detail_not_dict_rejected(self):
        """detail 必须是 dict"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": [
                {"type": "api", "detail": "GET /foo"}  # detail 是字符串
            ],
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_permissions_not_list_rejected(self):
        """permissions 必须是数组"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": {"type": "api", "detail": {}},  # dict 不是 list
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "permissions" in str(exc.value).lower()

    def test_validate_manifest_permissions_empty_type_rejected(self):
        """type 不能是空字符串"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": [{"type": "", "detail": {"method": "GET"}}],
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_permissions_valid_shape_ok(self):
        """合法形状的 permissions 应通过"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": [
                {"type": "api", "detail": {"method": "GET", "path": "/api/v1/foo"}},
                {"type": "menu", "detail": {"target": "inject:sidemenu"}},
                {"type": "db_table", "detail": {"name": "app_data_foo"}},
            ],
        }
        version_service.validate_manifest(manifest)  # 不抛即通过

    def test_validate_manifest_permissions_empty_list_ok(self):
        """空 permissions 数组应通过"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": [],
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_permissions_absent_ok(self):
        """无 permissions 字段应通过（可选）"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_permissions_non_dict_item_rejected(self):
        """permissions 数组里的元素必须是 dict"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "permissions": ["api"],  # 字符串，不是 dict
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    # ---------- pages/models 模式一致性校验（spec 6.2 决策）----------

    def test_validate_manifest_single_table_mode_ok(self):
        """单表模式：顶层 data_schema，pages 无 model"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"name": {"type": "string", "default": ""}},
                "required": ["name"],
            },
            "pages": [
                {"key": "list", "title": "List", "page_type": "table"},
                {"key": "form", "title": "Form", "page_type": "form"},
            ],
        }
        version_service.validate_manifest(manifest)  # 不抛即通过

    def test_validate_manifest_single_table_mode_explicit_underscore_ok(self):
        """单表模式下 page.model='_' 显式声明也允许"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"x": {"type": "string", "default": ""}},
                "required": ["x"],
            },
            "pages": [
                {"key": "list", "title": "List", "page_type": "table", "model": "_"},
            ],
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_multi_table_mode_ok(self):
        """多表模式：models[] + pages 引用 model key"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "models": [
                {
                    "key": "contact",
                    "data_schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "default": ""}},
                        "required": ["name"],
                    },
                },
                {
                    "key": "company",
                    "data_schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "default": ""}},
                        "required": ["name"],
                    },
                },
            ],
            "pages": [
                {
                    "key": "list",
                    "title": "Contacts",
                    "page_type": "table",
                    "model": "contact",
                },
                {
                    "key": "form",
                    "title": "Contact Form",
                    "page_type": "form",
                    "model": "contact",
                },
                {
                    "key": "companies",
                    "title": "Companies",
                    "page_type": "table",
                    "model": "company",
                },
            ],
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_mixed_data_schema_and_models_rejected(self):
        """data_schema + models[] 同时存在 → 拒绝（模式混用）"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"x": {"type": "string", "default": ""}},
                "required": ["x"],
            },
            "models": [
                {
                    "key": "contact",
                    "data_schema": {
                        "type": "object",
                        "properties": {"y": {"type": "string", "default": ""}},
                        "required": ["y"],
                    },
                },
            ],
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "data_schema" in str(exc.value)
        assert "models" in str(exc.value)

    def test_validate_manifest_single_table_with_page_model_rejected(self):
        """关键回归：单表模式 + page.model → 拒绝（之前的 404 bug 根因）"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {"name": {"type": "string", "default": ""}},
                "required": ["name"],
            },
            "pages": [
                {
                    "key": "list",
                    "title": "List",
                    "page_type": "table",
                    "model": "contact",
                },
            ],
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        msg = str(exc.value)
        assert "pages[0].model" in msg
        assert "models" in msg

    def test_validate_manifest_multi_table_undeclared_model_rejected(self):
        """多表模式下 page.model 必须在 models[] 中声明"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "models": [
                {
                    "key": "contact",
                    "data_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "string", "default": ""}},
                        "required": ["x"],
                    },
                },
            ],
            "pages": [
                {"key": "list", "title": "X", "page_type": "table", "model": "unknown"},
            ],
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "unknown" in str(exc.value)

    def test_validate_manifest_multi_table_page_missing_model_rejected(self):
        """多表模式下 page.model 必填"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "models": [
                {
                    "key": "contact",
                    "data_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "string", "default": ""}},
                        "required": ["x"],
                    },
                },
            ],
            "pages": [
                {"key": "list", "title": "X", "page_type": "table"},  # 缺 model
            ],
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "pages[0]" in str(exc.value)
        assert "model" in str(exc.value)

    def test_validate_manifest_models_duplicate_key_rejected(self):
        """models[] 的 key 不能重复"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "models": [
                {
                    "key": "contact",
                    "data_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "string", "default": ""}},
                        "required": ["x"],
                    },
                },
                {
                    "key": "contact",  # 重复
                    "data_schema": {
                        "type": "object",
                        "properties": {"y": {"type": "string", "default": ""}},
                        "required": ["y"],
                    },
                },
            ],
        }
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "重复" in str(exc.value)

    def test_validate_manifest_models_missing_key_rejected(self):
        """models[] 每项必须有 key"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "models": [
                {
                    "data_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "string", "default": ""}},
                        "required": ["x"],
                    },
                },
            ],
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_no_data_no_pages_ok(self):
        """纯展示型应用：无 data_schema / models / pages → 通过"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "frontend",
            "category": "tool",
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_pages_not_list_rejected(self):
        """pages 不是数组 → 拒绝"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "pages": {"key": "list"},
        }
        with pytest.raises(AppInvalidManifestException):
            version_service.validate_manifest(manifest)

    def test_validate_manifest_missing_required_field(self):
        manifest = {"name": "X", "slug": "valid-slug"}  # 缺 version/type/category
        with pytest.raises(AppInvalidManifestException) as exc:
            version_service.validate_manifest(manifest)
        assert "version" in str(exc.value)

    def test_validate_manifest_semver_with_prerelease_ok(self):
        """1.0.0-rc.1 应通过（semver 后缀允许）"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0-rc.1",
            "type": "lowcode",
            "category": "business",
        }
        version_service.validate_manifest(manifest)

    def test_validate_manifest_without_data_schema_ok(self):
        """缺 data_schema 不应报错"""
        manifest = {
            "name": "X",
            "slug": "valid-slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        }
        version_service.validate_manifest(manifest)


@pytest.fixture
async def sample_app(db_session):
    """共享样本应用：每个测试拿到一个干净的 App（依赖 db_session 回滚）"""
    app = App(
        tenant_id=0,
        name="版本测试应用",
        slug="version-test-app",
        type="lowcode",
        category="business",
        status="published",
    )
    db_session.add(app)
    await db_session.flush()
    return app


class TestVersionServiceCRUD:
    """覆盖 create / get_by_version / get_latest_approved 的数据库集成"""

    async def test_create_and_get_by_version(self, db_session, sample_app):
        manifest = {
            "name": "X",
            "slug": "version-test-app",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        }
        v = await version_service.create(
            db_session,
            app_id=sample_app.id,
            version="1.0.0",
            manifest=manifest,
            file_url="/uploads/marketplace/foo/1.0.0/foo.zip",
            file_hash="a" * 64,
            file_size=100,
        )
        await db_session.flush()
        assert v.id is not None
        assert v.review_status == "pending"

        fetched = await version_service.get_by_version(
            db_session, app_id=sample_app.id, version="1.0.0"
        )
        assert fetched.id == v.id
        assert fetched.file_hash == "a" * 64

    async def test_get_by_version_not_found(self, db_session, sample_app):
        with pytest.raises(AppNotFoundException):
            await version_service.get_by_version(
                db_session, app_id=sample_app.id, version="9.9.9"
            )

    async def test_get_latest_approved_returns_none_when_no_approved(
        self, db_session, sample_app
    ):
        """没有 approved 版本时应返回 None"""
        result = await version_service.get_latest_approved(
            db_session, app_id=sample_app.id
        )
        assert result is None

    async def test_get_latest_approved_returns_approved_only(
        self, db_session, sample_app
    ):
        """只查 review_status='approved' 的版本"""
        manifest = {
            "name": "X",
            "slug": "version-test-app",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        }
        # 1.0.0 还在 pending
        v1 = await version_service.create(
            db_session,
            app_id=sample_app.id,
            version="1.0.0",
            manifest=manifest,
            file_url="/u/1.zip",
            file_hash="a" * 64,
        )
        await db_session.flush()
        assert v1.review_status == "pending"

        latest = await version_service.get_latest_approved(
            db_session, app_id=sample_app.id
        )
        assert latest is None  # 没有 approved 的
