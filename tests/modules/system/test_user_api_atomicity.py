"""update_user 端点事务原子性测试。

回归：旧实现先 `await user_service.update_user(...)` 再 `await db.commit()`，
然后才校验 dept_ids。校验失败抛异常时，user 表已落库，前端看到错误
弹窗以为没改成功，实际数据已变。

修复后必须先校验 dept_ids 再 update，整个事务一次 commit。
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.system.api.user import update_user
from app.modules.system.schemas.user import UserDeptItem, UserUpdate


def _dept_item(*, dept_id: str = "1", is_primary: bool = False) -> UserDeptItem:
    return UserDeptItem(dept_id=dept_id, is_primary=is_primary)


async def test_dept_validation_runs_before_user_update():
    """dept_ids 缺主部门时，user_service.update_user 不应被调用。"""
    user_in = UserUpdate(user_name="alice", status="1", dept_ids=[_dept_item()])

    with (
        patch(
            "app.modules.system.api.user.user_service.update_user",
            new=AsyncMock(),
        ) as mock_update,
        patch(
            "app.modules.system.api.user.config_service.get_bool",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.modules.system.api.user.dept_service.update_user_depts",
            new=AsyncMock(),
        ) as mock_dept_update,
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await update_user(user_id=123, user_in=user_in, db=AsyncMock())

    assert exc_info.value.error_code == "USER_PRIMARY_DEPT_REQUIRED"
    mock_update.assert_not_awaited()
    mock_dept_update.assert_not_awaited()


async def test_user_update_runs_when_dept_validation_passes():
    """dept_ids 有主部门时，user_service.update_user 应被调用。"""
    user_in = UserUpdate(
        user_name="alice",
        status="1",
        dept_ids=[_dept_item(is_primary=True)],
    )

    db_mock = AsyncMock()

    with (
        patch(
            "app.modules.system.api.user.user_service.update_user",
            new=AsyncMock(),
        ) as mock_update,
        patch(
            "app.modules.system.api.user.config_service.get_bool",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.modules.system.api.user.dept_service.update_user_depts",
            new=AsyncMock(),
        ),
    ):
        await update_user(user_id=123, user_in=user_in, db=db_mock)

    mock_update.assert_awaited_once()
    db_mock.commit.assert_awaited()


async def test_user_update_runs_when_no_dept_ids_provided():
    """dept_ids=None 时跳过校验，user_service.update_user 应被调用。"""
    # dept_ids 默认是 []（空 list），不是 None。用 model_construct 绕过校验
    # 来模拟 "未提供 dept_ids" 的场景。
    user_in = UserUpdate.model_construct(user_name="alice", status="1")
    user_in.dept_ids = None

    db_mock = AsyncMock()

    with (
        patch(
            "app.modules.system.api.user.user_service.update_user",
            new=AsyncMock(),
        ) as mock_update,
        patch(
            "app.modules.system.api.user.config_service.get_bool",
            new=AsyncMock(return_value=True),
        ) as mock_get_bool,
        patch(
            "app.modules.system.api.user.dept_service.update_user_depts",
            new=AsyncMock(),
        ),
    ):
        await update_user(user_id=123, user_in=user_in, db=db_mock)

    mock_update.assert_awaited_once()
    # dept_ids is None 时不应查 config
    mock_get_bool.assert_not_awaited()
