"""scripts/sync_menus.py MENU_DEFINITIONS 静态校验。

验证 ``system:user:import`` / ``system:user:export`` 两个按钮权限已被 seed 到
MENU_DEFINITIONS，形状正确（``menu_type=F`` / ``parent_route=system_user`` /
``status=1`` / 非空 ``permission``）。

不调用 ``sync_menus()``：那是 IDLE 一次性的增量同步入口，跑它会真连 DB。
静态遍历 MENU_DEFINITIONS 即可证明 seed 已写入，部署侧执行
``python scripts/sync_menus.py`` 后会落到 sys_menu 表。
"""

import pytest

from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from scripts.sync_menus import MENU_DEFINITIONS


def _find_by_permission(perm: str) -> dict:
    """按 permission 找 MENU_DEFINITIONS 条目；找不到 fail with clear message。"""
    matches = [d for d in MENU_DEFINITIONS if d.get("permission") == perm]
    assert matches, f"permission {perm!r} not found in MENU_DEFINITIONS"
    assert len(matches) == 1, f"permission {perm!r} duplicated: {matches}"
    return matches[0]


class TestUserImportExportPermissionSeed:
    """sync_menus 必须包含导入和导出按钮权限种子。"""

    def test_import_permission_seeded(self):
        """system:user:import 在 MENU_DEFINITIONS 里。"""
        entry = _find_by_permission("system:user:import")
        assert entry["menu_type"] == "F"
        assert entry["parent_route"] == "system_user"
        assert entry["status"] == "1"
        # F-type 必须有 key（sync_menus 用 key 做 dedup 标识）
        assert entry.get("key"), f"import entry missing key: {entry}"
        # menu_name 非空（前端按钮文本）
        assert entry["menu_name"]

    def test_export_permission_seeded(self):
        """system:user:export 在 MENU_DEFINITIONS 里。"""
        entry = _find_by_permission("system:user:export")
        assert entry["menu_type"] == "F"
        assert entry["parent_route"] == "system_user"
        assert entry["status"] == "1"
        assert entry.get("key"), f"export entry missing key: {entry}"
        assert entry["menu_name"]

    def test_import_export_have_distinct_keys(self):
        """两个按钮 key 必须不同，避免 sync_menus dedup 误判为重复。"""
        import_entry = _find_by_permission("system:user:import")
        export_entry = _find_by_permission("system:user:export")
        assert import_entry["key"] != export_entry["key"]

    @pytest.mark.parametrize(
        "perm",
        [
            "system:user:import",
            "system:user:export",
        ],
    )
    def test_permission_attached_to_button_type_only(self, perm):
        """F-type 条目才能挂 permission；C/M 类型不该带（防 i18n_key 冲突）。"""
        entry = _find_by_permission(perm)
        assert entry["menu_type"] == "F", (
            f"{perm} must be menu_type=F (button), got {entry['menu_type']!r}"
        )

    def test_existing_user_list_permission_unchanged(self):
        """回归：原有 system:user:list 仍在（防 refactor 误删）。"""
        entry = _find_by_permission("system:user:list")
        assert entry["parent_route"] == "system_user"
        assert entry["menu_type"] == "F"


def test_ai_chat_permission_seeded_under_chat_menu() -> None:
    entry = _find_by_permission("ai:chat:use")

    assert entry["menu_type"] == "F"
    assert entry["parent_route"] == "ai_chat"
    assert entry["status"] == "1"
    assert entry.get("key")


def test_ai_file_parse_permission_seeded_under_chat_menu() -> None:
    entry = _find_by_permission("ai:file:parse")

    assert entry["menu_type"] == "F"
    assert entry["parent_route"] == "ai_chat"
    assert entry["status"] == "1"
    assert entry.get("key")


def test_user_role_auth_permission_seeded_under_user_menu() -> None:
    entry = _find_by_permission(USER_ROLE_AUTH_PERMISSION)

    assert entry["menu_type"] == "F"
    assert entry["parent_route"] == "system_user"
    assert entry["status"] == "1"
    assert entry.get("key")


def test_ai_trace_permission_is_an_independent_page_menu() -> None:
    entry = _find_by_permission("ai:trace:view")

    assert entry["menu_type"] == "C"
    assert entry["parent_route"] == "ai"
    assert entry["route_name"] == "ai_trace"
    assert entry["component"] == "view.ai_trace"
    assert entry["page"] == "ai_trace"
    assert entry["route_path"] == "/ai/trace"
    assert entry["i18n_key"] == "route.ai_trace"
    assert entry["status"] == "1"
