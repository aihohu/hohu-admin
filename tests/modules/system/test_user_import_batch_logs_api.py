"""``GET /system/user/import/{batch_id}/logs`` HTTP 契约测试。

验证批次操作日志的分页和事件过滤。

只验证 HTTP 契约层（路由 / 200 字段映射 / 404 / auth gating / event filter /
pagination 参数转发），service 层（``list_batch_logs``）用 patch 替身。
service 层的 ordering / outerjoin / 过滤逻辑由 ``test_user_import_execute.py``
等集成测试覆盖（写入侧 + 反查侧）。

覆盖：
- 401 未登录或 JWT 无效
- 200 + PageResult[UserImportBatchLogItem] 字段映射
- 200 + 空列表（batch 存在但无日志，理论不应发生但需容错）
- 200 + operator_name=None（操作人已删除，outerjoin 兼容）
- 404 + AI_IMPORT_BATCH_NOT_FOUND（batch 不存在）
- event filter 透传到 service
- pagination current/size 默认值 + 自定义值
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import select

from app.core.config import settings
from app.main import app
from app.modules.system.constants import ImportBatchStatus
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserImportBatchLog

#: service 模块路径（patch target）
_API_MODULE = "app.modules.system.api.user"


# ========== Fixtures ==========


@pytest.fixture
async def client(db_session):  # noqa: ARG001 (db_session resets redis)
    """ASGI test client（对齐 test_user_import_batch_detail_api.py 模式）。"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def admin_token(db_session) -> str:
    """admin 用户 JWT（admin 绕过 system:user:list 检查）。"""
    user = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    exp = datetime.now(UTC) + timedelta(hours=1)
    payload = {
        "exp": exp,
        "sub": str(user.user_id),
        "tid": str(user.tenant_id),
        "tver": "1",
        "type": "access",
        "user_id": user.user_id,
        "user_name": user.user_name,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ========== Helpers ==========


def _make_log_row(
    *,
    log_id: str = "log-001",
    batch_id: str = "batch-abc-001",
    operator_id: int = 1,
    event: str = "CREATED",
    from_status: ImportBatchStatus | None = None,
    to_status: ImportBatchStatus | None = ImportBatchStatus.CREATED,
    detail: dict | None = None,
    created_at: datetime | None = None,
) -> UserImportBatchLog:
    """构造 UserImportBatchLog ORM 实例替身（patch 返回值）。"""
    return UserImportBatchLog(
        log_id=log_id,
        batch_id=batch_id,
        operator_id=operator_id,
        event=event,
        from_status=from_status,
        to_status=to_status,
        detail=detail or {"filename": "users.xlsx", "total_rows": 100},
        created_at=created_at or datetime(2026, 8, 1, 14, 0, 0),
    )


def _patch_batch_exists():
    """patch get_batch_detail 返回非 None（避免 404 拦截 list 调用）。"""
    return patch(
        f"{_API_MODULE}.get_batch_detail",
        new=AsyncMock(return_value=("batch-row-placeholder", "admin")),
    )


# ========== Auth ==========


class TestGetBatchLogsAuth:
    """验证 system:user:list 权限。"""

    async def test_no_token_returns_401(self, client):
        response = await client.get("/system/user/import/batch-abc-001/logs")
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        response = await client.get(
            "/system/user/import/batch-abc-001/logs",
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert response.status_code == 401


# ========== 200 字段映射 ==========


class TestGetBatchLogsResponse:
    """返回事件、状态迁移、详情和创建时间。"""

    async def test_returns_paginated_logs_with_field_mapping(self, client, admin_token):
        """200 + PageResult[UserImportBatchLogItem]，records 字段映射正确。"""
        log1 = _make_log_row(
            log_id="log-001",
            event="CREATED",
            from_status=None,
            to_status=ImportBatchStatus.CREATED,
            detail={"filename": "users.xlsx", "total_rows": 100},
            created_at=datetime(2026, 8, 1, 14, 0, 0),
        )
        log2 = _make_log_row(
            log_id="log-002",
            event="EXECUTE_FINISH",
            from_status=ImportBatchStatus.RUNNING,
            to_status=ImportBatchStatus.SUCCESS,
            detail={"success_count": 100, "failed_count": 0},
            created_at=datetime(2026, 8, 1, 14, 1, 30),
        )
        with (
            _patch_batch_exists(),
            patch(
                f"{_API_MODULE}.list_batch_logs",
                new=AsyncMock(return_value=([(log1, "admin"), (log2, "admin")], 2)),
            ),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001/logs",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 200
        data = body["data"]
        # 分页元数据
        assert data["total"] == 2
        assert data["current"] == 1
        assert data["size"] == 10
        assert len(data["records"]) == 2

        rec0 = data["records"][0]
        # 日志核心字段。
        assert rec0["logId"] == "log-001"
        assert rec0["event"] == "CREATED"
        assert rec0["fromStatus"] is None
        assert rec0["toStatus"] == "CREATED"
        assert rec0["detail"] == {"filename": "users.xlsx", "total_rows": 100}
        assert rec0["createdAt"].startswith("2026-08-01T14:00:00")
        # 审计补充字段（logId/operatorId/operatorName）
        assert rec0["operatorId"] == "1"  # Snowflake 字符串化
        assert rec0["operatorName"] == "admin"

        rec1 = data["records"][1]
        assert rec1["event"] == "EXECUTE_FINISH"
        assert rec1["fromStatus"] == "RUNNING"
        assert rec1["toStatus"] == "SUCCESS"
        assert rec1["detail"] == {"success_count": 100, "failed_count": 0}

    async def test_returns_empty_list_when_no_logs(self, client, admin_token):
        """200 + records=[] + total=0（batch 存在但无日志，需容错）。"""
        with (
            _patch_batch_exists(),
            patch(
                f"{_API_MODULE}.list_batch_logs",
                new=AsyncMock(return_value=([], 0)),
            ),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001/logs",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 0
        assert data["records"] == []

    async def test_operator_name_none_for_deleted_user(self, client, admin_token):
        """outerjoin sys_user 后，操作人已删除时 operator_name=None。
        user 删除不级联删 log，审计完整性优先）。"""
        log = _make_log_row(operator_id=999, event="CREATED")
        with (
            _patch_batch_exists(),
            patch(
                f"{_API_MODULE}.list_batch_logs",
                new=AsyncMock(return_value=([(log, None)], 1)),
            ),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001/logs",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        rec = response.json()["data"]["records"][0]
        assert rec["operatorId"] == "999"
        assert rec["operatorName"] is None


# ========== 404 ==========


class TestGetBatchLogsNotFound:
    """batch_id 不存在时返回 404 AI_IMPORT_BATCH_NOT_FOUND。"""

    async def test_batch_not_found_returns_404(self, client, admin_token):
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(None, None)),
        ):
            response = await client.get(
                "/system/user/import/nonexistent-batch/logs",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 404
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_BATCH_NOT_FOUND"


# ========== Event Filter ==========


class TestGetBatchLogsEventFilter:
    """event 参数可过滤 CREATED、PREVIEW_DONE、EXECUTE_START 等事件。"""

    async def test_event_filter_passed_to_service(self, client, admin_token):
        """?event=EXECUTE_FINISH 透传到 service.event 参数。"""
        with (
            _patch_batch_exists(),
            patch(
                f"{_API_MODULE}.list_batch_logs",
                new=AsyncMock(return_value=([], 0)),
            ) as mock_list,
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001/logs?event=EXECUTE_FINISH",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        mock_list.assert_awaited_once()
        assert mock_list.call_args.kwargs.get("event") == "EXECUTE_FINISH"

    async def test_no_event_passes_none(self, client, admin_token):
        """不传 event → service.event=None（不过滤）。"""
        with (
            _patch_batch_exists(),
            patch(
                f"{_API_MODULE}.list_batch_logs",
                new=AsyncMock(return_value=([], 0)),
            ) as mock_list,
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001/logs",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        assert mock_list.call_args.kwargs.get("event") is None


# ========== Pagination ==========


class TestGetBatchLogsPagination:
    """分页参数 current/size 默认 1/10，可自定义（同 export task 列表）。"""

    async def test_default_current_size(self, client, admin_token):
        """不传分页参数 → current=1, size=10。"""
        with (
            _patch_batch_exists(),
            patch(
                f"{_API_MODULE}.list_batch_logs",
                new=AsyncMock(return_value=([], 0)),
            ) as mock_list,
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001/logs",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        assert mock_list.call_args.kwargs.get("current") == 1
        assert mock_list.call_args.kwargs.get("size") == 10

    async def test_custom_current_size(self, client, admin_token):
        """?current=2&size=20 透传到 service。"""
        with (
            _patch_batch_exists(),
            patch(
                f"{_API_MODULE}.list_batch_logs",
                new=AsyncMock(return_value=([], 0)),
            ) as mock_list,
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001/logs?current=2&size=20",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        assert mock_list.call_args.kwargs.get("current") == 2
        assert mock_list.call_args.kwargs.get("size") == 20
