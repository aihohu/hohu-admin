"""``GET /system/user/import`` HTTP 契约测试。

验证分页、状态过滤和 ``system:user:list`` 权限。

只验证 HTTP 契约层（路由 / 字段映射 / 状态码 / auth gating / 过滤参数透传），
service 层（``list_batches``）用 patch 替身。完整业务流程（DB 真实查询 / outerjoin
sys_user / 排序稳定性）在 ``test_user_import_service.py`` 已覆盖。

覆盖：
- 401 未登录 / 无效 JWT（auth gating）
- 200 + PageResult[UserImportBatchResponse] 字段映射
- query 透传 operator_id / status / start_time / end_time 到 service
- 非法 status → service 抛 BusinessRuleException(AI_IMPORT_INVALID_STATUS) → 400
- 默认分页（current=1, size=10）
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.main import app
from app.modules.system.models.user import User
from app.modules.system.user.constants import ImportBatchStatus
from app.modules.system.user.models import UserImportBatch

#: service 模块路径（patch target）
_API_MODULE = "app.modules.system.api.user"


# ========== Fixtures ==========


@pytest.fixture
async def client(db_session):  # noqa: ARG001 (db_session resets redis)
    """ASGI test client（对齐 test_user_import_cancel_api.py 模式）。"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def admin_token(db_session) -> str:
    """admin 用户 JWT（admin 绕过 system:user:list 检查 + is_super_admin）。"""
    user = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    exp = datetime.now(UTC) + timedelta(hours=1)
    payload = {
        "exp": exp,
        "sub": str(user.user_id),
        "user_id": user.user_id,
        "user_name": user.user_name,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ========== Helpers ==========


def _make_batch_row(
    *,
    batch_id: str = "batch-abc-001",
    status: ImportBatchStatus = ImportBatchStatus.PARTIAL_SUCCESS,
    operator_id: int = 1,
    filename: str = "users_20260801.xlsx",
    total_rows: int = 100,
    summary_new: int = 80,
    summary_exists: int = 10,
    summary_conflict: int = 5,
    summary_out_of_scope: int = 5,
    success_count: int = 80,
    skipped_count: int = 10,
    overwritten_count: int = 0,
    failed_count: int = 10,
    created_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> UserImportBatch:
    """构造 UserImportBatch ORM 替身（list_batches 返回元组的 batch 部分）。

    显式传所有 summary_*/count 字段：ORM ``default=0`` 仅在 flush 时触发，
    直接实例化时这些字段为 ``None``，Pydantic 验证会失败。
    """
    return UserImportBatch(
        batch_id=batch_id,
        operator_id=operator_id,
        preview_token=f"preview-token-{batch_id}",
        file_storage_key=f"import-preview/{batch_id}.xlsx",
        filename=filename,
        file_sha256="sha256-placeholder",
        records_hash="records-hash-placeholder",
        total_rows=total_rows,
        summary_new=summary_new,
        summary_exists=summary_exists,
        summary_conflict=summary_conflict,
        summary_out_of_scope=summary_out_of_scope,
        success_count=success_count,
        skipped_count=skipped_count,
        overwritten_count=overwritten_count,
        failed_count=failed_count,
        on_conflict="skip",
        reason="2026年8月 HR 入职名单同步",
        status=status,
        created_at=created_at or datetime(2026, 8, 1, 14, 0, 0),
        finished_at=finished_at or datetime(2026, 8, 1, 14, 1, 30),
    )


# ========== Auth ==========


class TestListBatchesAuth:
    """验证 system:user:list 权限。"""

    async def test_no_token_returns_401(self, client):
        response = await client.get("/system/user/import")
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        response = await client.get(
            "/system/user/import",
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert response.status_code == 401


# ========== Pagination + 字段映射 ==========


class TestListBatchesResponse:
    """验证分页和 UserImportBatchResponse 字段。"""

    async def test_returns_paginated_list_with_response_fields(
        self, client, admin_token
    ):
        """200 + PageResult[UserImportBatchResponse]，records 字段完整。

        service 替身返 ``([(batch, operator_name), ...], total)``，
        API 层每个 batch 复用 ``_build_batch_response`` 构造响应 dict。
        """
        rows = [
            (
                _make_batch_row(
                    batch_id="batch-001",
                    status=ImportBatchStatus.PARTIAL_SUCCESS,
                    total_rows=100,
                ),
                "admin",
            ),
            (
                _make_batch_row(
                    batch_id="batch-002",
                    status=ImportBatchStatus.RUNNING,
                    total_rows=50,
                    finished_at=None,
                ),
                "alice",
            ),
        ]
        with patch(
            f"{_API_MODULE}.list_batches",
            new=AsyncMock(return_value=(rows, 2)),
        ) as mock_list:
            response = await client.get(
                "/system/user/import?current=1&size=10",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 200
        data = body["data"]
        assert data["total"] == 2
        assert data["current"] == 1
        assert data["size"] == 10
        assert len(data["records"]) == 2

        first = data["records"][0]
        assert first["batchId"] == "batch-001"
        assert first["status"] == "PARTIAL_SUCCESS"
        assert first["operatorId"] == "1"  # Snowflake 字符串化
        assert isinstance(first["operatorId"], str)
        assert first["operatorName"] == "admin"
        assert first["filename"] == "users_20260801.xlsx"
        assert first["totalRows"] == 100
        # expires_at 动态计算（PARTIAL_SUCCESS → finished_at + 24h）
        assert first["expiresAt"].startswith("2026-08-02T14:01:30")
        # 剥离预检凭证等敏感字段。
        assert "previewToken" not in first
        assert "fileSha256" not in first
        assert "recordsHash" not in first
        assert "reason" not in first

        # query 透传到 service
        mock_list.assert_awaited_once()
        query_arg = mock_list.call_args.args[1]
        assert query_arg.current == 1
        assert query_arg.size == 10

    async def test_default_pagination_when_no_query_params(self, client, admin_token):
        """无 query 参数 → 默认 current=1, size=10 透传到 service。"""
        with patch(
            f"{_API_MODULE}.list_batches",
            new=AsyncMock(return_value=([], 0)),
        ) as mock_list:
            response = await client.get(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        query_arg = mock_list.call_args.args[1]
        assert query_arg.current == 1
        assert query_arg.size == 10
        assert query_arg.operator_id is None
        assert query_arg.status is None
        assert query_arg.start_time is None
        assert query_arg.end_time is None


# ========== 过滤参数透传 ==========


class TestListBatchesFilters:
    """验证 operator_id、status 和 created_at 时间窗过滤。"""

    async def test_passes_status_and_operator_filters_to_service(
        self, client, admin_token
    ):
        """status / operatorId 透传到 UserImportBatchQuery。"""
        with patch(
            f"{_API_MODULE}.list_batches",
            new=AsyncMock(return_value=([], 0)),
        ) as mock_list:
            await client.get(
                "/system/user/import?current=2&size=20&status=PARTIAL_SUCCESS&operatorId=1",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        query_arg = mock_list.call_args.args[1]
        assert query_arg.current == 2
        assert query_arg.size == 20
        assert query_arg.status == "PARTIAL_SUCCESS"
        assert query_arg.operator_id == 1

    async def test_passes_created_at_range_to_service(self, client, admin_token):
        """startTime / endTime（ms timestamp）→ LocalNaiveDatetime → service。"""
        # 2026-08-01 00:00:00 local = ms timestamp
        start_ms = int(datetime(2026, 8, 1, 0, 0, 0).timestamp() * 1000)
        end_ms = int(datetime(2026, 8, 31, 23, 59, 59).timestamp() * 1000)
        with patch(
            f"{_API_MODULE}.list_batches",
            new=AsyncMock(return_value=([], 0)),
        ) as mock_list:
            await client.get(
                f"/system/user/import?startTime={start_ms}&endTime={end_ms}",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        query_arg = mock_list.call_args.args[1]
        # LocalNaiveDatetime 已转 naive datetime（CLAUDE.md pitfall 12）
        assert query_arg.start_time is not None
        assert query_arg.end_time is not None
        assert query_arg.start_time.year == 2026
        assert query_arg.start_time.month == 8
        assert query_arg.start_time.day == 1


# ========== 非法 status → 400 ==========


class TestListBatchesInvalidStatus:
    """service 层抛 BusinessRuleException(AI_IMPORT_INVALID_STATUS) → HTTP 400。

    决策 15c.x：status 校验在 service 层（不在 Pydantic Literal），
    与 ``list_export_tasks`` 完全对称（决策 export.x）—— 同用
    ``BusinessRuleException``（默认 code=400），不用 ``UnprocessableEntityException``。
    原因：跨端点一致性 > 单条 spec 文字推敲；前端 i18n 走 errorCode 不走 HTTP 码。
    """

    async def test_invalid_status_returns_400(self, client, admin_token):
        with patch(
            f"{_API_MODULE}.list_batches",
            new=AsyncMock(
                side_effect=BusinessRuleException(
                    "非法 status 值：INVALID",
                    error_code="AI_IMPORT_INVALID_STATUS",
                )
            ),
        ):
            response = await client.get(
                "/system/user/import?status=INVALID",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 400
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_INVALID_STATUS"
