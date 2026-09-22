"""Regression tests for data-scope demo login identities."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.sql.dml import Delete, Insert

from app.utils.validators import validate_user_name
from scripts.seed_demo_data_scope import (
    DEMO_MENU_ROUTE_NAMES,
    USERS,
    _assign_role_menus,
)


class _Result:
    def __init__(self, values=(), *, rowcount=0) -> None:
        self.values = list(values)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def first(self):
        return self.values[0] if self.values else None

    def all(self):
        return self.values


def test_seeded_demo_usernames_match_the_login_contract() -> None:
    for _user_id, user_name, *_rest in USERS:
        assert validate_user_name(user_name) == user_name


def test_demo_roles_receive_the_page_and_its_ancestor_directory() -> None:
    assert DEMO_MENU_ROUTE_NAMES == frozenset(
        {"home", "system", "system_data-scope-demo"}
    )


@pytest.mark.asyncio
async def test_demo_role_menu_reconciliation_removes_stale_grants_first() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _Result([800000020]),
            _Result([800000001, 800000002, 800000003]),
            _Result(rowcount=4),
            _Result(rowcount=15),
        ]
    )

    await _assign_role_menus(db)

    statements = [call.args[0] for call in db.execute.await_args_list]
    assert isinstance(statements[2], Delete)
    assert isinstance(statements[3], Insert)
