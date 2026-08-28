"""User import/export helpers 行为测试。

覆盖：
- OVERWRITE_NEVER / OVERWRITE_ALLOWED 不交集（防同一字段被误归类）
- OVERWRITE_NEVER 必含 user_id / user_name / hashed_password（安全锚点）
- OVERWRITE_ALLOWED 包含 employee_no
- EXPORT_ALLOWED_FIELDS 不含 hashed_password / employee_no（导出最小化）
- get_default_password：配置存在返回值 / 缺失抛 AI_IMPORT_DEFAULT_PASSWORD_NOT_SET

依赖 db_session outer-transaction fixture（不落库）。
"""

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.system.constants import (
    EXPORT_ALLOWED_FIELDS,
    OVERWRITE_ALLOWED,
    OVERWRITE_NEVER,
)
from app.modules.system.models.config import Config
from app.modules.system.service.user_service import (
    INSECURE_DEFAULT_PASSWORD_SENTINELS,
    get_default_password,
)


@pytest.fixture(autouse=True)
async def _cleanup_default_password_rows(db_session):
    """Clear any persisted sys_config.auth:default_password rows so the test's
    INSERT does not collide with the unique key.

    Why: db_session is outer-transaction rollback (no test pollution), but the
    dev DB itself may already hold a seeded auth:default_password row (init_db.py
    leaves one). A test INSERT of the same config_key triggers a unique-key
    violation regardless of transaction isolation. DELETE first, then INSERT.
    """
    await db_session.execute(
        delete(Config).where(Config.config_key == "auth:default_password")
    )
    await db_session.flush()


class TestOverwriteConstants:
    """OVERWRITE_* 常量静态校验（防 typo / 防误归类）。"""

    def test_overwrite_never_and_allowed_disjoint(self):
        """永不覆盖的字段不应出现在 ALLOWED 中。"""
        assert OVERWRITE_NEVER & OVERWRITE_ALLOWED == frozenset(), (
            "OVERWRITE_NEVER ∩ OVERWRITE_ALLOWED 必须为空，否则 on_conflict=overwrite "
            "时敏感字段（如 hashed_password）会被覆盖"
        )

    def test_overwrite_never_contains_security_anchors(self):
        """user_id、user_name 和 hashed_password 必须在 NEVER 集合。"""
        for required in ("user_id", "user_name", "hashed_password"):
            assert required in OVERWRITE_NEVER, (
                f"{required} 必须在 OVERWRITE_NEVER（防 identity / 凭据被覆盖）"
            )

    def test_overwrite_allowed_contains_employee_no(self):
        """employee_no 位于 ALLOWED，支持 HR 修改工号。"""
        assert "employee_no" in OVERWRITE_ALLOWED

    def test_overwrite_allowed_excludes_security_anchors(self):
        """防 spec 演进时误把敏感字段加到 ALLOWED。"""
        assert "hashed_password" not in OVERWRITE_ALLOWED
        assert "user_id" not in OVERWRITE_ALLOWED


class TestExportAllowedFields:
    """EXPORT_ALLOWED_FIELDS 白名单测试。"""

    def test_export_excludes_sensitive_fields(self):
        """hashed_password 和 employee_no 不进入 Excel。"""
        assert "hashed_password" not in EXPORT_ALLOWED_FIELDS
        assert "employee_no" not in EXPORT_ALLOWED_FIELDS

    def test_export_contains_safe_fields(self):
        """白名单包含所有必要展示字段。"""
        required = {
            "user_name",
            "nickname",
            "user_email",
            "user_phone",
            "dept_id",
            "role_codes",
            "user_gender",
            "status",
            "create_time",
        }
        assert required <= EXPORT_ALLOWED_FIELDS

    def test_export_allowed_fields_is_frozen_set(self):
        """白名单必须是 frozenset（防运行时被业务代码污染）。"""
        assert isinstance(EXPORT_ALLOWED_FIELDS, frozenset)


class TestGetDefaultPassword:
    """get_default_password helper 行为测试。

    所有导入用户用 sys_config.auth:default_password 哈希入库。
    缺失时抛 AI_IMPORT_DEFAULT_PASSWORD_NOT_SET（防硬编码默认密码安全风险）。
    """

    async def test_returns_value_when_configured(self, db_session):
        db_session.add(
            Config(
                config_id=1,
                config_name="默认密码",
                config_key="auth:default_password",
                config_value="Welcome@2026",
                config_group="auth",
                status="1",
            )
        )
        await db_session.flush()

        value = await get_default_password(db_session)
        assert value == "Welcome@2026"

    async def test_raises_when_missing(self, db_session):
        """未配置 → 抛 AI_IMPORT_DEFAULT_PASSWORD_NOT_SET（强制 admin 显式配置）。"""
        with pytest.raises(BusinessRuleException) as exc:
            await get_default_password(db_session)
        assert exc.value.error_code == "AI_IMPORT_DEFAULT_PASSWORD_NOT_SET"

    async def test_raises_when_disabled(self, db_session):
        """status='2'（禁用）等价于未配置（防 admin 关掉默认密码但忘了清配置）。"""
        db_session.add(
            Config(
                config_id=2,
                config_name="默认密码",
                config_key="auth:default_password",
                config_value="Welcome@2026",
                config_group="auth",
                status="2",  # 禁用
            )
        )
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await get_default_password(db_session)
        assert exc.value.error_code == "AI_IMPORT_DEFAULT_PASSWORD_NOT_SET"

    async def test_returns_latest_when_multiple_rows(self, db_session):
        """config_key UNIQUE 约束保证只有一行，但 helper 行为不应受重复影响。"""
        db_session.add(
            Config(
                config_id=3,
                config_name="默认密码",
                config_key="auth:default_password",
                config_value="Hohu@Init#2026",
                config_group="auth",
                status="1",
            )
        )
        await db_session.flush()

        value = await get_default_password(db_session)
        assert value == "Hohu@Init#2026"

    async def test_prod_rejects_public_seed_password(
        self, db_session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_seed = next(iter(INSECURE_DEFAULT_PASSWORD_SENTINELS))
        db_session.add(
            Config(
                config_id=4,
                config_name="默认密码",
                config_key="auth:default_password",
                config_value=public_seed,
                config_group="auth",
                status="1",
            )
        )
        await db_session.flush()
        monkeypatch.setattr(settings, "ENV", "prod")

        with pytest.raises(BusinessRuleException) as exc:
            await get_default_password(db_session)

        assert exc.value.error_code == "AI_IMPORT_DEFAULT_PASSWORD_INVALID"
