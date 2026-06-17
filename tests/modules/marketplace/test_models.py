from sqlalchemy import Numeric, inspect
from sqlalchemy.dialects.postgresql import JSONB

from app.modules.marketplace.models.app import App, AppVersion
from app.modules.marketplace.models.install import TenantApp
from app.modules.marketplace.models.permission import AppPermission
from app.modules.marketplace.models.rating import AppRating
from app.modules.marketplace.models.review import AppReview


class TestAppModel:
    def test_table_name(self):
        assert App.__tablename__ == "mk_app"

    def test_required_columns(self):
        column_names = {c.key for c in inspect(App).columns}
        required = {
            "id",
            "tenant_id",
            "name",
            "slug",
            "type",
            "category",
            "description",
            "icon",
            "author_id",
            "author_name",
            "status",
            "current_version_id",
            "homepage",
            "license",
            "download_count",
            "avg_rating",
            "rating_count",
            "tags_text",
            "created_at",
            "updated_at",
        }
        assert required.issubset(column_names), f"missing: {required - column_names}"

    def test_slug_max_length_150(self):
        slug_col = next(c for c in inspect(App).columns if c.key == "slug")
        assert slug_col.type.length == 150

    def test_avg_rating_decimal_3_1(self):
        col = next(c for c in inspect(App).columns if c.key == "avg_rating")
        assert isinstance(col.type, Numeric)
        assert col.type.precision == 3
        assert col.type.scale == 1

    def test_avg_rating_has_check_constraint(self):
        table = App.__table__
        check_defs = [
            str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext")
        ]
        assert any("avg_rating" in d for d in check_defs)


class TestAppVersionModel:
    def test_table_name(self):
        assert AppVersion.__tablename__ == "mk_app_version"

    def test_manifest_is_jsonb(self):
        col = next(c for c in inspect(AppVersion).columns if c.key == "manifest")
        assert isinstance(col.type, JSONB)

    def test_no_review_id_field(self):
        """Spec decision: removed review_id as anti-pattern (双向 FK)"""
        column_names = {c.key for c in inspect(AppVersion).columns}
        assert "review_id" not in column_names


class TestAppReviewModel:
    def test_table_name(self):
        assert AppReview.__tablename__ == "mk_app_review"


class TestTenantAppModel:
    def test_table_name(self):
        assert TenantApp.__tablename__ == "mk_tenant_app"

    def test_status_default_is_installed(self):
        """Spec decision #10: DEFAULT 'installed'（不是 disabled）"""
        col = next(c for c in inspect(TenantApp).columns if c.key == "status")
        assert col.default is not None
        assert "installed" in str(col.default.arg) or col.default.arg == "installed"

    def test_has_retained_table_names_jsonb(self):
        col = next(
            c for c in inspect(TenantApp).columns if c.key == "retained_table_names"
        )
        assert isinstance(col.type, JSONB)


class TestAppPermissionModel:
    def test_table_name(self):
        assert AppPermission.__tablename__ == "mk_app_permission"

    def test_has_detail_canonical_field(self):
        column_names = {c.key for c in inspect(AppPermission).columns}
        assert "detail_canonical" in column_names
        assert "detail_hash" in column_names


class TestAppRatingModel:
    def test_table_name(self):
        assert AppRating.__tablename__ == "mk_app_rating"

    def test_rating_check_constraint(self):
        table = AppRating.__table__
        check_defs = [
            str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext")
        ]
        assert any("rating" in d and "BETWEEN" in d for d in check_defs)
