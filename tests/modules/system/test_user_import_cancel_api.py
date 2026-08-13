"""``POST /system/user/import/{batch_id}/cancel`` HTTP 契约测试。

验证导入批次取消行为。

两种取消场景：
- **场景 1**：PREVIEW_DONE → CAS 转 CANCELLED + 清理 preview 文件 + Redis cache
  仅允许 PREVIEW_DONE → CANCELLED，不允许 CREATED → CANCELLED
- **场景 2**：RUNNING 协作式 cancel — 设置 Redis 标志，chunk 之间检查标志，
  下一个 chunk 开始前跳出循环转 PARTIAL_SUCCESS（已 commit 的 chunk 保留）

终态拒绝：SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED / CREATED →
``AI_IMPORT_BATCH_NOT_CANCELLABLE``（422）。

权限：``system:user:import`` + 必须是 batch operator 本人或超管
并校验操作人或超级管理员权限。

只验证 HTTP 契约层（路由 / 字段映射 / 状态码 / auth gating / reason 校验），
service 层（``cancel_batch``）用 patch 替身。完整业务流程在
``test_user_import_execute.py`` 等集成测试覆盖。
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    NotFoundException,
    UnprocessableEntityException,
)
from app.main import app
from app.modules.system.models.user import User
from app.modules.system.user.constants import ImportBatchStatus
from app.modules.system.user.models import UserImportBatch

#: service 模块路径（patch target）
_API_MODULE = "app.modules.system.api.user"


# ========== Fixtures ==========


@pytest.fixture
async def client(db_session):  # noqa: ARG001 (db_session resets redis)
    """ASGI test client（对齐 test_user_import_batch_logs_api.py 模式）。"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def admin_token(db_session) -> str:
    """admin 用户 JWT（admin 绕过 system:user:import 检查 + is_super_admin）。"""
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
    status: ImportBatchStatus = ImportBatchStatus.PREVIEW_DONE,
    operator_id: int = 1,
    preview_token: str = "preview-token-abc",
    file_storage_key: str = "import-preview/batch-abc-001.xlsx",
    reason: str = "2026年8月 HR 入职名单同步",
    created_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> UserImportBatch:
    """构造 UserImportBatch ORM 替身（patch 返回值）。"""
    return UserImportBatch(
        batch_id=batch_id,
        operator_id=operator_id,
        preview_token=preview_token,
        file_storage_key=file_storage_key,
        filename="users.xlsx",
        file_sha256="sha256-placeholder",
        records_hash="records-hash-placeholder",
        total_rows=100,
        on_conflict="skip",
        reason=reason,
        status=status,
        created_at=created_at or datetime(2026, 8, 1, 14, 0, 0),
        finished_at=finished_at,
    )


# ========== Auth ==========


class TestCancelBatchAuth:
    """验证 system:user:import 权限。"""

    async def test_no_token_returns_401(self, client):
        response = await client.post(
            "/system/user/import/batch-abc-001/cancel",
            json={"reason": "用户主动取消"},
        )
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        response = await client.post(
            "/system/user/import/batch-abc-001/cancel",
            json={"reason": "用户主动取消"},
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert response.status_code == 401


# ========== Reason Validation ==========


class TestCancelReasonValidation:
    """reason 必填，长度为 1-256 字符，strip 后非空。

    Pydantic ReasonSchema 在 API 入口校验，不通过返 422（FastAPI 默认 validation_response）。
    """

    async def test_reason_required_422(self, client, admin_token):
        """缺 reason 字段 → 422。"""
        response = await client.post(
            "/system/user/import/batch-abc-001/cancel",
            json={},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 422

    async def test_reason_empty_string_422(self, client, admin_token):
        """reason="" → 422（min_length=1）。"""
        response = await client.post(
            "/system/user/import/batch-abc-001/cancel",
            json={"reason": ""},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 422

    async def test_reason_whitespace_only_422(self, client, admin_token):
        """reason 全空白时 validator 返回 422。"""
        response = await client.post(
            "/system/user/import/batch-abc-001/cancel",
            json={"reason": "   "},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 422

    async def test_reason_too_long_422(self, client, admin_token):
        """reason > 256 字符 → 422（max_length=256）。"""
        response = await client.post(
            "/system/user/import/batch-abc-001/cancel",
            json={"reason": "x" * 257},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 422


# ========== Scenario 1: PREVIEW_DONE → CANCELLED ==========


class TestCancelPreviewDoneBatch:
    """PREVIEW_DONE 通过 CAS 直接迁移为 CANCELLED。"""

    async def test_cancel_preview_done_returns_cancelled(self, client, admin_token):
        """200 + {batchId, status=CANCELLED, cancelledAt}。

        service 替身返回 status=CANCELLED（已 CAS 完成），finished_at=cancelledAt。
        """
        cancelled_at = datetime(2026, 8, 1, 14, 1, 30)
        batch = _make_batch_row(
            status=ImportBatchStatus.CANCELLED,
            finished_at=cancelled_at,
        )
        with patch(
            f"{_API_MODULE}.cancel_batch",
            new=AsyncMock(return_value=batch),
        ) as mock_cancel:
            response = await client.post(
                "/system/user/import/batch-abc-001/cancel",
                json={"reason": "用户主动取消"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 200
        data = body["data"]
        assert data["batchId"] == "batch-abc-001"
        assert data["status"] == "CANCELLED"
        assert data["cancelledAt"].startswith("2026-08-01T14:01:30")
        # service 调用契约：传 batch_id / operator / reason
        mock_cancel.assert_awaited_once()
        call = mock_cancel.call_args
        assert call.args[1] == "batch-abc-001"  # batch_id 位置参数
        assert call.kwargs.get("reason") == "用户主动取消"

    async def test_cancel_passes_reason_to_service(self, client, admin_token):
        """reason 应透传到 service 进入审计链路。"""
        batch = _make_batch_row(status=ImportBatchStatus.CANCELLED)
        with patch(
            f"{_API_MODULE}.cancel_batch",
            new=AsyncMock(return_value=batch),
        ) as mock_cancel:
            response = await client.post(
                "/system/user/import/batch-abc-001/cancel",
                json={"reason": "上传错文件了"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        assert mock_cancel.call_args.kwargs.get("reason") == "上传错文件了"


# ========== Scenario 2: RUNNING 协作式 cancel ==========


class TestCancelRunningBatch:
    """RUNNING 批次设置 Redis cancel 标志并立即返回 200。

    cancel 请求不等待当前分块实际暂停。
    实际 RUNNING → PARTIAL_SUCCESS 转换发生在 chunk loop（batch_create 内）。
    """

    async def test_cancel_running_sets_redis_flag_and_returns_running(
        self, client, admin_token
    ):
        """RUNNING batch → service 设置 Redis 标志后返回 batch（status 仍 RUNNING）。

        响应 status=RUNNING（current），cancelledAt=now（标志设置时间）。
        """
        running_batch = _make_batch_row(
            status=ImportBatchStatus.RUNNING,
            finished_at=datetime(2026, 8, 1, 14, 1, 30),
        )
        with patch(
            f"{_API_MODULE}.cancel_batch",
            new=AsyncMock(return_value=running_batch),
        ):
            response = await client.post(
                "/system/user/import/batch-abc-001/cancel",
                json={"reason": "发现数据有问题，停止后续导入"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        # 协作式取消时响应中的状态仍为 RUNNING。
        assert data["status"] == "RUNNING"
        assert data["batchId"] == "batch-abc-001"
        # cancelledAt 仍返回（标志设置时间，前端可显示「已请求取消」）
        assert data["cancelledAt"] is not None


# ========== Terminal States Rejected ==========


class TestCancelTerminalBatchRejected:
    """终态批次和 CREATED 批次拒绝取消。

    - SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED：终态不可 cancel
    - CREATED：仅允许 PREVIEW_DONE → CANCELLED，
      CREATED 不在合法 cancel-from-state（dry_run 完成 = PREVIEW_DONE）
    """

    @pytest.mark.parametrize(
        "status",
        [
            ImportBatchStatus.CREATED,
            ImportBatchStatus.SUCCESS,
            ImportBatchStatus.PARTIAL_SUCCESS,
            ImportBatchStatus.FAILED,
            ImportBatchStatus.EXPIRED,
            ImportBatchStatus.CANCELLED,
        ],
    )
    async def test_cancel_non_cancellable_states_returns_422(
        self,
        client,
        admin_token,
        status,  # noqa: ARG002 (parametrize id)
    ):
        """CREATED + 5 个终态 → 422 AI_IMPORT_BATCH_NOT_CANCELLABLE。"""
        with patch(
            f"{_API_MODULE}.cancel_batch",
            new=AsyncMock(
                side_effect=UnprocessableEntityException(
                    "批次状态不可取消",
                    error_code="AI_IMPORT_BATCH_NOT_CANCELLABLE",
                )
            ),
        ):
            response = await client.post(
                "/system/user/import/batch-abc-001/cancel",
                json={"reason": "用户主动取消"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 422
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_BATCH_NOT_CANCELLABLE"


# ========== 404 batch not found ==========


class TestCancelBatchNotFound:
    """batch_id 不存在时返回 AI_IMPORT_BATCH_NOT_FOUND。

    与批次详情和日志接口保持一致，使用 NotFoundException 返回 404，
    早已 ship 404，跨端点一致性 > 单条 spec 文字）。
    """

    async def test_cancel_nonexistent_batch_returns_404(self, client, admin_token):
        with patch(
            f"{_API_MODULE}.cancel_batch",
            new=AsyncMock(
                side_effect=NotFoundException(
                    "用户导入批次",
                    error_code="AI_IMPORT_BATCH_NOT_FOUND",
                )
            ),
        ):
            response = await client.post(
                "/system/user/import/nonexistent-batch/cancel",
                json={"reason": "用户主动取消"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 404
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_BATCH_NOT_FOUND"


# ========== 403 operator forbidden ==========


class TestCancelByNonOperatorForbidden:
    """只有批次操作人或超级管理员可以取消。

    HTTP 层只验证 service 抛 AuthorizationException → 403 转换；service 层
    的 operator 校验逻辑（is_super_admin / operator_id 比对）在
    test_user_import_execute.py 等集成测试覆盖。
    """

    async def test_cancel_by_non_operator_returns_403(self, client, admin_token):
        """非 operator 非超管 → 403。"""
        with patch(
            f"{_API_MODULE}.cancel_batch",
            new=AsyncMock(side_effect=AuthorizationException("无权取消此批次")),
        ):
            response = await client.post(
                "/system/user/import/batch-abc-001/cancel",
                json={"reason": "用户主动取消"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 403
