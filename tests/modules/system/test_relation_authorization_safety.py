"""Before/after checks preserve live repair paths and complete descendant boundaries."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.authorization_plan import ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import BusinessException
from app.modules.system.service.relation_authorization_safety import (
    REPAIR_CODES,
    require_manager_preserved,
    validate_menu_delegation,
)


def facts():
    result = {k: [] for k in K}
    result[K.ROLE] = [{"role_id": 201, "role_code": "R_SUPER", "status": "1"}]
    result[K.USER] = [{"user_id": 101, "status": "1"}]
    result[K.USER_ROLES] = [{"user_id": 101, "role_id": 201}]
    result[K.MENU] = [
        {"menu_id": 501, "parent_id": None, "menu_type": "M", "status": "1"},
        {
            "menu_id": 502,
            "parent_id": 501,
            "menu_type": "C",
            "status": "1",
            "route_name": "system_role",
            "component": "view.system_role",
            "route_path": "/system/role",
        },
        *(
            {
                "menu_id": i,
                "parent_id": 502,
                "menu_type": "F",
                "status": "1",
                "permission": p,
            }
            for i, p in enumerate(sorted(REPAIR_CODES), 503)
        ),
    ]
    result[K.ROLE_MENUS] = [
        {"role_id": 201, "menu_id": m["menu_id"]} for m in result[K.MENU]
    ]
    return result


@pytest.mark.parametrize(
    "change",
    [
        "user",
        "role",
        "membership",
        "ancestor",
        "component",
        "button",
        "hidden",
        "cycle",
        "unbound",
    ],
)
def test_last_repair_path_cannot_be_lost(change):
    before = facts()
    after = deepcopy(before)
    if change == "user":
        after[K.USER][0]["status"] = "2"
    if change == "role":
        after[K.ROLE][0]["status"] = "2"
    if change == "membership":
        after[K.USER_ROLES].clear()
    if change == "ancestor":
        after[K.MENU][0]["status"] = "2"
    if change == "component":
        after[K.MENU][1]["component"] = "view.other"
    if change == "button":
        after[K.MENU][-1]["permission"] = "system:other:edit"
    if change == "hidden":
        after[K.MENU][1]["hide_in_menu"] = True
    if change == "cycle":
        after[K.MENU][0]["parent_id"] = 502
    if change == "unbound":
        after[K.ROLE_MENUS] = [r for r in after[K.ROLE_MENUS] if r["menu_id"] != 501]
    with pytest.raises(BusinessException) as error:
        require_manager_preserved(before, after)
    assert (
        error.value.error_code == "AUTHORIZATION_LAST_MANAGER_REQUIRED"
        and error.value.code == 409
    )


def test_damaged_registered_navigation_requires_actual_repair():
    damaged = facts()
    damaged[K.MENU][1]["status"] = "2"
    with pytest.raises(BusinessException):
        require_manager_preserved(damaged, deepcopy(damaged))
    require_manager_preserved(damaged, facts())


def test_another_enabled_repair_account_can_preserve_access():
    before = facts()
    before[K.ROLE].append({"role_id": 202, "role_code": "R_SUPER", "status": "1"})
    before[K.USER].append({"user_id": 102, "status": "1"})
    before[K.USER_ROLES].append({"user_id": 102, "role_id": 202})
    before[K.ROLE_MENUS] += [
        {"role_id": 202, "menu_id": m["menu_id"]} for m in before[K.MENU]
    ]
    after = deepcopy(before)
    after[K.ROLE][0]["status"] = "2"
    require_manager_preserved(before, after)


@pytest.mark.parametrize(
    "values", [{"parent_id": None}, {"status": "1"}, {"permission": "outside:grant"}]
)
def test_registration_uses_full_subtree_and_member_scope(values):
    before = facts()
    before[K.ROLE][0]["role_code"] = "R_STAFF"
    actor = SimpleNamespace(
        super_admin=False,
        permission_codes=frozenset(REPAIR_CODES),
        enabled_role_ids=frozenset({999}),
        accessible_user_scope=frozenset({999}),
        menu_ids=frozenset(m["menu_id"] for m in before[K.MENU]),
    )
    change = SimpleNamespace(
        key=ResourceKey(K.MENU, 0, 501), action="update", values=values
    )
    with pytest.raises(BusinessException) as error:
        validate_menu_delegation(before, before, [change], actor)
    assert error.value.error_code == "AUTHORIZATION_DELEGATION_DENIED"


def test_ordinary_icon_change_does_not_confer_registration():
    before = facts()
    actor = SimpleNamespace(
        super_admin=False,
        permission_codes=frozenset(),
        enabled_role_ids=frozenset(),
        accessible_user_scope=frozenset(),
        menu_ids=frozenset(),
    )
    change = SimpleNamespace(
        key=ResourceKey(K.MENU, 0, 501), action="update", values={"icon": "icon:new"}
    )
    validate_menu_delegation(before, before, [change], actor)
