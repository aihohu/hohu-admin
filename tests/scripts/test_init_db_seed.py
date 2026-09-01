"""scripts/init_db.py 种子数据静态校验。

验证初始化种子包含：
1. ``system:user:import`` / ``system:user:export`` 两个按钮权限 Menu
   （fresh install 立刻有，无需后续 sync_menus）
2. 两个按钮的 ``parent_id`` 指向 ``system_user`` 菜单的 ``menu_id``
   （防 orphan button：parent_id=0 时前端菜单树挂不上去）
3. ``sys_config.auth:default_password`` 种子 Config 对象
   导入使用全局默认密码；helper 缺失时抛
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
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.system.constants import DEPT_MOVE_PERMISSION, USER_ROLE_AUTH_PERMISSION
from app.utils.validators import validate_password
from scripts.init_db import (
    build_default_tenant,
    build_init_roles,
    default_password_seed_value,
    fresh_role_permission_menus,
    init_configs,
    init_menus,
)


def test_default_tenant_seed_uses_reserved_identity_and_enabled_status():
    tenant = build_default_tenant()

    assert tenant.tenant_id == 0
    assert tenant.tenant_code == "default"
    assert tenant.status == STATUS_ENABLED
    assert tenant.row_version == 1


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
    """导入和导出按钮权限的 parent_id 指向 system_user。"""

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


class TestConfigSeed:
    """验证 sys_config.auth:default_password 种子。

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
        """remark 包含修改默认密码的安全提示。"""
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

    def test_primary_department_policy_has_a_safe_fresh_default(self):
        """Fresh installs must seed the lockable primary-department policy."""
        matches = [
            config
            for config in init_configs
            if config.config_key == "user_require_primary_dept"
        ]

        assert len(matches) == 1
        config = matches[0]
        assert config.config_value == "false"
        assert config.status == STATUS_ENABLED
        assert config.is_public is False


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

    def test_super_admin_gets_all_published_agent_permissions(self):
        admin_role, _ = build_init_roles()

        admin_role.menus = fresh_role_permission_menus(init_menus)

        assert {menu.permission for menu in admin_role.menus} == {
            "ai:agent:edit",
            "ai:agent:list",
            "ai:chat:use",
            "ai:file:parse",
            "system:dept:add",
            "system:dept:batch-delete",
            "system:dept:delete",
            "system:dept:edit",
            "system:dept:list",
            DEPT_MOVE_PERMISSION,
            "system:role:add",
            "system:role:ai-agent-auth",
            "system:role:batch-delete",
            "system:role:delete",
            "system:role:edit",
            "system:role:list",
            "system:role:menu-auth",
            "system:user:add",
            "system:user:delete",
            "system:user:edit",
            "system:user:export",
            "system:user:import",
            "system:user:list",
            "system:user:reset-password",
            USER_ROLE_AUTH_PERMISSION,
        }

        parent_routes = {
            "system:dept": "system_dept",
            "system:role": "system_role",
            "system:user": "system_user",
        }
        menus_by_id = {menu.menu_id: menu for menu in init_menus}
        for menu in admin_role.menus:
            prefix = ":".join(menu.permission.split(":")[:2])
            route_name = parent_routes.get(prefix)
            if route_name is None:
                continue
            parent = menus_by_id[menu.parent_id]
            assert parent.route_name == route_name

    def test_user_role_auth_permission_is_seeded_under_user_menu(self):
        button = _find_menu_by_permission(init_menus, USER_ROLE_AUTH_PERMISSION)
        parent = _find_menu_by_route_name(init_menus, "system_user")

        assert button.menu_type == "F"
        assert button.status == STATUS_ENABLED
        assert button.parent_id == parent.menu_id


class TestMenuPartitions:
    def test_fresh_seed_preserves_auth_system_and_task_roots(self):
        """Fresh menus must preserve the established domain grouping."""
        routes = {
            menu.route_name: menu for menu in init_menus if menu.route_name is not None
        }

        assert {"auth", "system", "task"} <= routes.keys()
        for route_name in (
            "system_dept",
            "system_user",
            "system_role",
            "system_menu",
        ):
            assert routes[route_name].parent_id == routes["auth"].menu_id
        for route_name in ("system_job", "system_job-log"):
            assert routes[route_name].parent_id == routes["task"].menu_id


class TestAiChatPermissionSeed:
    def test_role_agent_authorization_uses_canonical_menu_label(self):
        menu = _find_menu_by_permission(init_menus, "system:role:ai-agent-auth")

        assert menu.menu_name == "AI Agent 授权"

    def test_every_builtin_tool_permission_exists_in_fresh_menu_seed(self):
        """Fresh startup must satisfy Registry permission referential integrity."""
        load_builtin_tools()
        required_permissions = {
            permission
            for tool in ToolRegistry.get().all()
            for permission in tool.meta.required_perms
        }
        seeded_permissions = {
            menu.permission for menu in init_menus if menu.permission is not None
        }

        assert required_permissions <= seeded_permissions, (
            required_permissions - seeded_permissions
        )

    def test_job_edit_permission_is_seeded_under_job_menu(self):
        """The job tool permission must remain attached to its page menu."""
        button = _find_menu_by_permission(init_menus, "system:job:edit")
        parent = _find_menu_by_route_name(init_menus, "system_job")

        assert button.menu_type == "F"
        assert button.status == STATUS_ENABLED
        assert button.parent_id == parent.menu_id

    def test_ai_chat_permission_is_seeded_under_chat_menu(self):
        button = _find_menu_by_permission(init_menus, "ai:chat:use")
        parent = _find_menu_by_route_name(init_menus, "ai_chat")

        assert button.menu_type == "F"
        assert button.status == STATUS_ENABLED
        assert button.parent_id == parent.menu_id

    def test_file_parse_permission_is_seeded_under_chat_menu(self):
        button = _find_menu_by_permission(init_menus, "ai:file:parse")
        parent = _find_menu_by_route_name(init_menus, "ai_chat")

        assert button.menu_type == "F"
        assert button.status == STATUS_ENABLED
        assert button.parent_id == parent.menu_id

    def test_agent_permissions_are_seeded_under_agent_menu(self):
        parent = _find_menu_by_route_name(init_menus, "ai_agent")
        for permission in ("ai:agent:list", "ai:agent:edit"):
            button = _find_menu_by_permission(init_menus, permission)
            assert button.parent_id == parent.menu_id

    def test_trace_permission_is_seeded_as_an_independent_page(self):
        page = _find_menu_by_permission(init_menus, "ai:trace:view")

        assert page.menu_type == "C"
        assert page.parent_id == _find_menu_by_route_name(init_menus, "ai").menu_id
        assert page.route_name == "ai_trace"
        assert page.component == "view.ai_trace"
        assert page.route_path == "/ai/trace"

    def test_file_parse_is_enabled_for_fresh_install(self):
        config = next(c for c in init_configs if c.config_key == "ai:enabled_tools")
        assert config.config_value == '["file.parse"]'
