"""GET /system/user/import/{batch_id} HTTP 契约测试（Task 15，spec §5.4）。

spec §5.4 v2.2 P2 line 2229-2278：按 batch_id 查导入结果（前端导入历史 +
Phase 3 异步轮询 + 审计反查）。

只验证 HTTP 契约层（路由 / 200 字段映射 / 404 / auth gating），
service 层（get_batch_detail）用 patch 替身。完整业务流程在
``test_user_import_service.py`` / ``test_user_import_api.py`` 已覆盖。

覆盖：
- 401 未登录 / 无效 JWT（auth gating，spec §5.4 line 2234）
- 200 + UserImportBatchResponse 字段映射（spec §5.4 line 2238-2264）
- 404 + AI_IMPORT_BATCH_NOT_FOUND（spec §5.7 错误码表）
- expires_at 计算（spec §5.4 line 2262 + §2.19 preview TTL 10min）
- sync_mode=None（决策：当前不查 batch_log，spec §5.4 line 2258 字段保留）
- operator_name 反查 sys_user（spec §5.4 line 2246）
- system:user:list 权限（spec §5.4 line 2234：list 即可）
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import select

from app.core.config import settings
from app.main import app
from app.modules.system.models.user import User
from app.modules.system.user.constants import ImportBatchStatus
from app.modules.system.user.models import UserImportBatch

#: service 模块路径（patch target）
_API_MODULE = "app.modules.system.api.user"


# ========== Fixtures ==========


@pytest.fixture
async def client(db_session):  # noqa: ARG001 (db_session resets redis)
    """ASGI test client。

    依赖 ``db_session`` 触发 ``tests/modules/system/conftest.py`` 的
    ``_reset_redis_client()``，刷新 audit_middleware / auth.service 的 redis_client
    绑定到当前 loop（与 test_user_export_api.py / test_user_import_api.py 同款）。
    """
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
    total_rows: int = 2000,
    summary_new: int = 1500,
    summary_exists: int = 400,
    summary_conflict: int = 50,
    summary_out_of_scope: int = 50,
    success_count: int = 1500,
    skipped_count: int = 400,
    overwritten_count: int = 0,
    failed_count: int = 100,
    failed_rows_file: str | None = "/file/import-error/batch-abc-001.xlsx",
    on_conflict: str = "skip",
    reason: str = "2026年8月 HR 入职名单同步",
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> UserImportBatch:
    """构造一个 UserImportBatch ORM 实例替身。

    默认值对齐 spec §5.4 line 2238-2264 示例（PARTIAL_SUCCESS，1500/400/50/50/100）。
    """
    now = datetime(2026, 8, 1, 14, 0, 0)
    return UserImportBatch(
        batch_id=batch_id,
        operator_id=operator_id,
        filename=filename,
        file_sha256="sha256placeholder",
        records_hash="sha256recordsplaceholder",
        total_rows=total_rows,
        preview_token="preview-token-placeholder",
        summary_new=summary_new,
        summary_exists=summary_exists,
        summary_conflict=summary_conflict,
        summary_out_of_scope=summary_out_of_scope,
        success_count=success_count,
        skipped_count=skipped_count,
        overwritten_count=overwritten_count,
        failed_count=failed_count,
        failed_rows_file=failed_rows_file,
        on_conflict=on_conflict,
        reason=reason,
        status=status,
        created_at=created_at or now,
        started_at=started_at or now + timedelta(minutes=1),
        finished_at=finished_at or now + timedelta(minutes=2),
    )


# ========== Auth ==========


class TestGetBatchDetailAuth:
    """spec §5.4 line 2234：权限 system:user:list（list 即可）。"""

    async def test_no_token_returns_401(self, client):
        response = await client.get("/system/user/import/batch-abc-001")
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        response = await client.get(
            "/system/user/import/batch-abc-001",
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert response.status_code == 401


# ========== 200 字段映射 ==========


class TestGetBatchDetailResponse:
    """spec §5.4 line 2238-2264：返回 batch 详情。"""

    async def test_returns_batch_fields(self, client, admin_token):
        """200 + 全部字段映射正确（spec §5.4 line 2238-2264）。"""
        batch = _make_batch_row()
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 200
        data = body["data"]

        # 核心标识符 + 状态
        assert data["batchId"] == "batch-abc-001"
        assert data["status"] == "PARTIAL_SUCCESS"
        assert data["filename"] == "users_20260801.xlsx"
        assert data["onConflict"] == "skip"

        # 操作人（spec §5.4 line 2245-2246）
        assert data["operatorId"] == "1"  # Snowflake 字符串化
        assert data["operatorName"] == "admin"

        # 计数（spec §5.4 line 2247-2255）
        assert data["totalRows"] == 2000
        assert data["summaryNew"] == 1500
        assert data["summaryExists"] == 400
        assert data["summaryConflict"] == 50
        assert data["summaryOutOfScope"] == 50
        assert data["successCount"] == 1500
        assert data["skippedCount"] == 400
        assert data["overwrittenCount"] == 0
        assert data["failedCount"] == 100

        # 失败行文件（spec §5.4 line 2256）
        assert data["failedRowsFile"] == "/file/import-error/batch-abc-001.xlsx"

        # 时间（spec §5.4 line 2259-2262，ISO 8601）
        assert data["createdAt"].startswith("2026-08-01T14:00:00")
        assert data["startedAt"].startswith("2026-08-01T14:01:00")
        assert data["finishedAt"].startswith("2026-08-01T14:02:00")
        # expiresAt 存在（具体值由 TestGetBatchDetailExpiresAt 覆盖）
        assert "expiresAt" in data
        assert data["expiresAt"] is not None

    async def test_does_not_expose_sensitive_fields(self, client, admin_token):
        """spec §5.4 line 2238-2264 + 安全：不返 preview_token / file_sha256 /
        records_hash / reason（reason 进入审计链路但 GET 详情不暴露给前端列表）。

        决策 15.x：preview_token 是 execute 凭证，泄露可被重放（spec §2.19）；
        file_sha256 / records_hash 是内部三重校验指纹，对前端无意义；
        reason 在 GET /import 列表场景下不宜返回（敏感），仅 audit 链路保留。
        """
        batch = _make_batch_row()
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        # 敏感字段不应出现在响应中
        assert "previewToken" not in data, (
            "preview_token 不应暴露给前端（spec §2.19 三重校验凭证）"
        )
        assert "fileSha256" not in data
        assert "recordsHash" not in data
        assert "reason" not in data, (
            "reason 不在 GET /import/{batch_id} 返回，仅审计链路保留"
        )

    async def test_operator_name_from_user_join(self, client, admin_token):
        """spec §5.4 line 2246：operatorName 是 join sys_user 拿的 user_name。

        service 层返回 (batch, operator_name)，API 层透传到 response.operator_name。
        """
        batch = _make_batch_row(operator_id=999)
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "hr_zhang")),
        ) as mock_service:
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["operatorId"] == "999"
        assert data["operatorName"] == "hr_zhang"
        # service 调用：传 batch_id 字符串
        mock_service.assert_awaited_once()
        assert mock_service.call_args.args[1] == "batch-abc-001"

    async def test_sync_mode_returns_none_when_not_in_batch(self, client, admin_token):
        """spec §5.4 line 2258 syncMode 字段：sync_mode 不在 batch 表（在
        batch_log.detail），决策：本版不查 batch_log，先返 None。

        字段保留为 spec 兼容（前端容错 null），后续 task 22 可补 batch_log 反查。
        """
        batch = _make_batch_row()
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert "syncMode" in data
        assert data["syncMode"] is None


# ========== 404 ==========


class TestGetBatchDetailNotFound:
    """spec §5.7：batch_id 不存在 → 404 AI_IMPORT_BATCH_NOT_FOUND。"""

    async def test_not_found_returns_404(self, client, admin_token):
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(None, None)),
        ):
            response = await client.get(
                "/system/user/import/nonexistent-batch",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 404
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_BATCH_NOT_FOUND"


# ========== expires_at 计算 ==========


class TestGetBatchDetailExpiresAt:
    """spec §5.4 line 2262 + §2.19 preview TTL 10min + 历史保留 24h。

    决策 15.x：expires_at 按状态动态算：
    - CREATED / PREVIEW_DONE：created_at + 10min（preview_token TTL，spec §2.19）
    - RUNNING：created_at + 10min（保险，RUNNING 不应长存）
    - SUCCESS / PARTIAL_SUCCESS / FAILED：finished_at + 24h（文件保留 24h，
      spec §3.x 失败行文件存储默认 1 天 TTL）
    - EXPIRED / CANCELLED：finished_at + 24h（若 finished_at 为 None，回退 created_at + 24h）
    """

    async def test_preview_state_expires_at_created_plus_10min(
        self, client, admin_token
    ):
        """CREATED → expires_at = created_at + 10min（spec §2.19 preview TTL）。"""
        created = datetime(2026, 8, 1, 14, 0, 0)
        batch = _make_batch_row(
            status=ImportBatchStatus.CREATED,
            created_at=created,
            started_at=None,
            finished_at=None,
        )
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        expires_at = response.json()["data"]["expiresAt"]
        # 14:10:00（10 分钟后）
        assert expires_at.startswith("2026-08-01T14:10:00"), (
            f"CREATED 状态 expires_at 应 = created_at + 10min: {expires_at}"
        )

    async def test_preview_done_state_expires_at_created_plus_10min(
        self, client, admin_token
    ):
        """PREVIEW_DONE → expires_at = created_at + 10min（同 CREATED，preview 窗口）。"""
        created = datetime(2026, 8, 1, 14, 0, 0)
        batch = _make_batch_row(
            status=ImportBatchStatus.PREVIEW_DONE,
            created_at=created,
            started_at=None,
            finished_at=None,
        )
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        expires_at = response.json()["data"]["expiresAt"]
        assert expires_at.startswith("2026-08-01T14:10:00"), (
            f"PREVIEW_DONE 状态 expires_at 应 = created_at + 10min: {expires_at}"
        )

    async def test_finished_state_expires_at_finished_plus_24h(
        self, client, admin_token
    ):
        """SUCCESS / PARTIAL_SUCCESS / FAILED → expires_at = finished_at + 24h。"""
        created = datetime(2026, 8, 1, 14, 0, 0)
        finished = datetime(2026, 8, 1, 14, 2, 30)
        batch = _make_batch_row(
            status=ImportBatchStatus.SUCCESS,
            created_at=created,
            finished_at=finished,
        )
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        expires_at = response.json()["data"]["expiresAt"]
        # 次日 14:02:30（24 小时后）
        assert expires_at.startswith("2026-08-02T14:02:30"), (
            f"SUCCESS 状态 expires_at 应 = finished_at + 24h: {expires_at}"
        )

    async def test_failed_state_expires_at_finished_plus_24h(self, client, admin_token):
        """FAILED → 同 finished 语义（finished_at + 24h）。"""
        created = datetime(2026, 8, 1, 14, 0, 0)
        finished = datetime(2026, 8, 1, 14, 5, 0)
        batch = _make_batch_row(
            status=ImportBatchStatus.FAILED,
            failed_rows_file=None,
            created_at=created,
            finished_at=finished,
        )
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        expires_at = response.json()["data"]["expiresAt"]
        assert expires_at.startswith("2026-08-02T14:05:00"), (
            f"FAILED 状态 expires_at 应 = finished_at + 24h: {expires_at}"
        )

    async def test_failed_rows_file_optional(self, client, admin_token):
        """spec §5.4 line 2256：failed_rows_file 可空（无失败行时）。"""
        batch = _make_batch_row(
            status=ImportBatchStatus.SUCCESS,
            failed_count=0,
            failed_rows_file=None,
        )
        with patch(
            f"{_API_MODULE}.get_batch_detail",
            new=AsyncMock(return_value=(batch, "admin")),
        ):
            response = await client.get(
                "/system/user/import/batch-abc-001",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["failedRowsFile"] is None
        assert data["failedCount"] == 0
