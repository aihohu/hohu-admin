"""scripts/init_db.py 种子数据静态校验（Task 16，spec §10 line 3095 + §2.5）。

验证初始化种子包含：
1. ``system:user:import`` / ``system:user:export`` 两个按钮权限 Menu
   （fresh install 立刻有，无需后续 sync_menus）
2. 两个按钮的 ``parent_id`` 指向 ``system_user`` 菜单的 ``menu_id``
   （防 orphan button：parent_id=0 时前端菜单树挂不上去）
3. ``sys_config.auth:default_password`` 种子 Config 对象
   （spec §2.5 line 212：导入用全局默认密码；helper 缺失时抛
   ``AI_IMPORT_DEFAULT_PASSWORD_NOT_SET``，所以 fresh install 必须先种好）

不调用 ``init_db()``：那需要 input() 交互 + DROP TABLE，跑它会清库。
直接检查 ``init_menus`` / ``init_configs`` 列表即可。
"""

import pytest

from app.constants import (
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
    USER_ROLE_CODE,
)
from app.utils.validators import validate_password
from scripts.init_db import (
    build_init_roles,
    default_password_seed_value,
    init_configs,
    init_menus,
)


def _find_menu_by_permission(menus: list, perm: str):
    """按 permission 找 Menu；找不到 fail。"""
    matches = [m for m in menus if getattr(m, "permission", None) == perm]
    assert matches, f"Menu with permission={perm!r} not found in init_menus"
    assert len(matches) == 1, f"permission {perm!r} duplicated: {matches}"
    return matches[0]


def _find_menu_by_route_name(menus: list, route_name: str):
    matches = [m for m in menus if getattr(m, "route_name", None) == route_name]
    assert matches, f"Menu with route_name={route_name!r} not found"
    return matches[0]


class TestUserButtonPermissionSeed:
    """spec §10 Task 16：2 个按钮权限 + parent_id 指向 system_user。"""

    def test_import_button_seeded(self):
        """init_menus 含 system:user:import F-type Menu。"""
        m = _find_menu_by_permission(init_menus, "system:user:import")
        assert m.menu_type == "F"
        assert m.menu_name  # 非空（前端按钮文本）
        assert m.status == "1"

    def test_export_button_seeded(self):
        """init_menus 含 system:user:export F-type Menu。"""
        m = _find_menu_by_permission(init_menus, "system:user:export")
        assert m.menu_type == "F"
        assert m.menu_name
        assert m.status == "1"

    def test_import_button_parent_links_to_system_user(self):
        """import 按钮 parent_id 必须 == system_user Menu 的 menu_id（防 orphan）。"""
        button = _find_menu_by_permission(init_menus, "system:user:import")
        parent = _find_menu_by_route_name(init_menus, "system_user")
        assert button.parent_id == parent.menu_id, (
            f"import button parent_id={button.parent_id} != "
            f"system_user menu_id={parent.menu_id}"
        )

    def test_export_button_parent_links_to_system_user(self):
        """export 按钮 parent_id 必须 == system_user Menu 的 menu_id。"""
        button = _find_menu_by_permission(init_menus, "system:user:export")
        parent = _find_menu_by_route_name(init_menus, "system_user")
        assert button.parent_id == parent.menu_id

    def test_no_duplicate_button_permissions(self):
        """init_menus 中 system:user:import / export 各只一条（防 seed 漂移）。"""
        perms = [getattr(m, "permission", None) for m in init_menus]
        assert perms.count("system:user:import") == 1
        assert perms.count("system:user:export") == 1


class TestDefaultPasswordConfigSeed:
    """spec §2.5 line 212 + Task 16：sys_config.auth:default_password seed。

    helper ``get_default_password`` 缺失抛 AI_IMPORT_DEFAULT_PASSWORD_NOT_SET，
    所以 fresh install 必须有种；否则首次导入直接报错，UX 差。
    """

    def test_default_password_config_seeded(self):
        """init_configs 含 config_key='auth:default_password' 一条。"""
        matches = [c for c in init_configs if c.config_key == "auth:default_password"]
        assert matches, "auth:default_password not in init_configs"
        assert len(matches) == 1, f"duplicated: {matches}"
        cfg = matches[0]

        # config_value 非空（否则 helper 仍会抛 NOT_SET）
        assert cfg.config_value, "default_password value must be non-empty"
        assert validate_password(cfg.config_value) == cfg.config_value
        # status='1' 启用（helper WHERE status='1' 过滤）
        assert cfg.status == "1", f"status must be '1', got {cfg.status!r}"
        # config_group 非空（模型 NOT NULL）
        assert cfg.config_group
        # config_name 非空
        assert cfg.config_name

    def test_prod_does_not_seed_a_usable_public_password(self):
        assert default_password_seed_value("prod") == ""
        assert validate_password(default_password_seed_value("dev"))

    def test_default_password_config_remark_warns_to_change(self):
        """remark 含安全提示，防部署方上线前忘记改默认密码（spec §2.5 反例 3）。"""
        matches = [c for c in init_configs if c.config_key == "auth:default_password"]
        assert matches
        cfg = matches[0]
        assert cfg.remark, "remark must be non-empty (security warning)"
        # remark 至少含一个安全相关关键词
        remark_lower = cfg.remark.lower()
        safety_keywords = ["修改", "change", "安全", "security", "production", "上线"]
        assert any(kw.lower() in remark_lower for kw in safety_keywords), (
            f"remark lacks safety keyword: {cfg.remark!r}"
        )

    def test_default_password_not_marked_public(self):
        """is_public=False（防未授权读取默认密码）。"""
        matches = [c for c in init_configs if c.config_key == "auth:default_password"]
        assert matches
        cfg = matches[0]
        assert cfg.is_public is False, (
            f"is_public must be False (sensitive config), got {cfg.is_public}"
        )

    @pytest.mark.parametrize(
        "key",
        [
            "auth:default_password",
        ],
    )
    def test_config_key_unique_in_seed(self, key):
        """同一 key 不能在 init_configs 出现两次（防 seed 漂移导致 UniqueViolation）。"""
        matches = [c for c in init_configs if c.config_key == key]
        assert len(matches) == 1


class TestRoleSeed:
    """初始化角色必须沿用现有数据库的 R_* 编码契约。"""

    def test_default_user_role_matches_existing_role_contract(self):
        assert USER_ROLE_CODE == "R_USER"

        roles = build_init_roles()
        roles_by_code = {role.role_code: role for role in roles}

        assert set(roles_by_code) == {SUPER_ADMIN_ROLE_CODE, USER_ROLE_CODE}
        default_role = roles_by_code[USER_ROLE_CODE]
        assert default_role.role_name == "普通用户"
        assert default_role.data_scope == DATA_SCOPE_SELF
        assert default_role.status == STATUS_ENABLED
