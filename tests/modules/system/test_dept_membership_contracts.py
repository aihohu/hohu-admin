"""Phase 2-B3 department-membership API contract tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.modules.system.api.dept import get_dept_users, update_dept_users
from app.modules.system.schemas.dept import DeptUsersOut, DeptUsersUpdate
from app.modules.system.service.user_department_assignment_service import (
    DepartmentMemberPage,
    DepartmentMemberRecord,
    DepartmentMembershipResult,
    user_department_assignment_service,
)


def test_department_member_ids_are_canonical_strings() -> None:
    body = DeptUsersUpdate.model_validate({"userIds": ["11", "12"]})

    assert body.user_ids == ["11", "12"]
    for invalid in ([11], ["01"], ["0"], ["11", "11"], None):
        with pytest.raises(ValidationError):
            DeptUsersUpdate.model_validate({"userIds": invalid})


def test_department_member_page_exposes_only_minimal_records() -> None:
    payload = DeptUsersOut.model_validate(
        {
            "current": 1,
            "size": 20,
            "total": 1,
            "records": [
                {
                    "userId": 11,
                    "userName": "alice",
                    "nickname": "Alice",
                    "status": "1",
                    "isMember": True,
                    "isPrimary": False,
                }
            ],
        }
    ).model_dump(mode="json", by_alias=True)

    assert payload == {
        "current": 1,
        "size": 20,
        "total": 1,
        "records": [
            {
                "userId": "11",
                "userName": "alice",
                "nickname": "Alice",
                "status": "1",
                "isMember": True,
                "isPrimary": False,
            }
        ],
    }
    assert "userEmail" not in payload["records"][0]
    assert "userPhone" not in payload["records"][0]


async def test_department_member_api_delegates_to_shared_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = DepartmentMemberPage(
        current=2,
        size=20,
        total=21,
        records=(
            DepartmentMemberRecord(
                user_id=11,
                user_name="alice",
                nickname="Alice",
                status="1",
                is_member=True,
                is_primary=False,
            ),
        ),
    )
    list_mock = AsyncMock(return_value=page)
    replace_mock = AsyncMock(
        return_value=DepartmentMembershipResult(added=1, removed=2)
    )
    monkeypatch.setattr(
        user_department_assignment_service,
        "list_department_members",
        list_mock,
    )
    monkeypatch.setattr(
        user_department_assignment_service,
        "replace_department_members",
        replace_mock,
    )
    db = AsyncMock()
    actor = SimpleNamespace(user_id=7)

    response = await get_dept_users(
        5,
        query="ali",
        current=2,
        size=20,
        db=db,
        current_user=actor,
    )
    await update_dept_users(
        5,
        DeptUsersUpdate(userIds=["11"]),
        db=db,
        current_user=actor,
    )

    assert response.data.records[0].user_id == 11
    list_mock.assert_awaited_once_with(
        db,
        actor_user_id=7,
        dept_id=5,
        query="ali",
        current=2,
        size=20,
    )
    replace_mock.assert_awaited_once_with(
        db,
        actor_user_id=7,
        dept_id=5,
        user_ids=["11"],
    )
    db.commit.assert_awaited_once()
