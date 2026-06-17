from datetime import datetime

import pytest
from pydantic import ValidationError

from app.core.id_generator import next_id
from app.modules.marketplace.models import App, AppRating
from app.modules.marketplace.schemas import (
    AppDetailOut,
    AppOut,
    AppQuery,
    InstallCreate,
    InstallOut,
    InstallQuery,
    PermissionOut,
    RatingCreate,
    RatingOut,
    VersionOut,
)


def _make_app(slug: str = "x") -> App:
    """构造未持久化的 App 实例（注入时间字段，绕过 server_default）"""
    now = datetime(2026, 6, 17, 12, 0, 0)
    app = App(
        id=next_id(),
        tenant_id=0,
        name="X",
        slug=slug,
        type="lowcode",
        category="business",
        status="published",
        download_count=0,
        avg_rating=0.0,
        rating_count=0,
    )
    app.created_at = now
    app.updated_at = now
    return app


class TestAppSchemas:
    def test_app_out_serializes_id_and_datetime_as_string(self):
        """AppOut.model_validate(App) 应将 Snowflake int ID 与 datetime 序列化为字符串"""
        app = _make_app()
        out = AppOut.model_validate(app)
        # model_validate 后字段仍是 int（来自 from_attributes）
        assert isinstance(out.id, int)
        # 序列化为 dict（API 响应路径）后变为字符串；by_alias 输出 camelCase
        dumped = out.model_dump(by_alias=True)
        assert isinstance(dumped["id"], str)
        assert dumped["id"].isdigit()
        assert isinstance(dumped["createdAt"], str)
        assert "2026" in dumped["createdAt"]

    def test_app_detail_out_optional_current_version_id_none(self):
        """AppDetailOut 允许 current_version_id 为 None；序列化后保持 None"""
        app = _make_app(slug="x-detail")
        out = AppDetailOut.model_validate(app)
        assert out.current_version_id is None
        assert out.tags == []
        dumped = out.model_dump(by_alias=True)
        assert dumped["currentVersionId"] is None

    def test_app_detail_out_serializes_current_version_id_when_set(self):
        app = _make_app(slug="x-detail-2")
        app.current_version_id = next_id()
        out = AppDetailOut.model_validate(app)
        dumped = out.model_dump(by_alias=True)
        assert isinstance(dumped["currentVersionId"], str)
        assert dumped["currentVersionId"].isdigit()

    def test_app_query_defaults(self):
        q = AppQuery()
        assert q.current == 1
        assert q.size == 10
        assert q.status == "published"
        assert q.sort == "download"

    def test_app_query_accepts_explicit_values(self):
        # 单词字段无 camelCase 别名（to_camel("current") == "current"）
        q = AppQuery(current=2, size=20, keyword="foo", status="draft")
        assert q.current == 2
        assert q.size == 20
        assert q.keyword == "foo"
        assert q.status == "draft"

    def test_version_out_type_signature(self):
        """VersionOut 类型签名占位，确认关键字段存在"""
        assert {"id", "app_id", "version", "manifest", "review_status"}.issubset(
            VersionOut.model_fields.keys()
        )


class TestInstallSchemas:
    def test_install_create_camel_alias(self):
        """InstallCreate 应支持 camelCase 输入 + snake_case 访问"""
        req = InstallCreate(appSlug="x")  # camelCase 输入
        assert req.app_slug == "x"  # snake_case 访问
        assert req.version is None
        assert req.approved_permissions == []
        assert req.config == {}

    def test_install_create_snake_case_via_name(self):
        """populate_by_name=True 也允许 snake_case 输入"""
        req = InstallCreate(app_slug="y", version="1.0.0")
        assert req.app_slug == "y"
        assert req.version == "1.0.0"

    def test_install_query_defaults(self):
        q = InstallQuery()
        assert q.current == 1
        assert q.size == 10
        assert q.status is None

    def test_install_out_signature(self):
        """InstallOut 类型签名占位"""
        assert {"id", "app_id", "installed_version", "status"}.issubset(
            InstallOut.model_fields.keys()
        )


class TestPermissionSchema:
    def test_permission_out(self):
        out = PermissionOut(type="api", detail={"method": "GET", "path": "/foo"})
        dumped = out.model_dump(by_alias=True)
        assert dumped["type"] == "api"
        assert dumped["detail"]["method"] == "GET"


class TestRatingSchemas:
    def test_rating_create_validates_app_id_numeric(self):
        with pytest.raises(ValidationError):
            RatingCreate(app_id="not-numeric", rating=5)

    def test_rating_create_validates_rating_range(self):
        with pytest.raises(ValidationError):
            RatingCreate(app_id="123", rating=0)  # < 1
        with pytest.raises(ValidationError):
            RatingCreate(app_id="123", rating=6)  # > 5

    def test_rating_create_valid_field(self):
        r = RatingCreate(app_id="12345", rating=5, comment="good")
        assert r.app_id == "12345"
        assert r.rating == 5

    def test_rating_out_serializes_ids_as_string(self):
        now = datetime(2026, 6, 17, 12, 0, 0)
        rating = AppRating(
            id=next_id(),
            app_id=next_id(),
            user_id=next_id(),
            rating=4,
            comment="ok",
        )
        rating.created_at = now
        out = RatingOut.model_validate(rating)
        dumped = out.model_dump(by_alias=True)
        assert isinstance(dumped["id"], str)
        assert dumped["id"].isdigit()
        assert isinstance(dumped["appId"], str)
        assert isinstance(dumped["userId"], str)
        assert out.rating == 4
        assert isinstance(dumped["createdAt"], str)
        assert "2026" in dumped["createdAt"]
