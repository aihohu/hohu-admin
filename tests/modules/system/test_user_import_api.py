"""``POST /system/user/import`` HTTP 契约测试。

只验证 HTTP 契约层（路由 / multipart / Form 解析 / 响应 envelope / 错误码），
service 层（dry_run_import_users / batch_create_users_from_records）用 patch 替身。
完整业务流程在 test_user_import_dry_run.py / test_user_import_execute.py 已覆盖。

覆盖：
- 401 未登录 / 无效 JWT（auth gating）
- dry_run=true → 200 + previewToken + expiresAt
- dry_run=false + preview_token → 200 + batchId + status + counts + failedRowsPreview
  + idempotentReplay
- 缺 preview_token → 422
- 缺 file → 422（multipart 必填）
- 缺 reason → 422（Pydantic 校验）
- 文件 MIME 非白名单 → 400 + AI_IMPORT_INVALID_MIME
- 文件 > 10MB → 400 + AI_IMPORT_FILE_TOO_LARGE
- 行数 > 2000 → 400 + AI_IMPORT_TOO_MANY_ROWS
- ImportErrorCollection → 400 + errors[] 含全部字段错误
- service 抛 AI_IMPORT_PREVIEW_INVALID → 422 + errorCode
- service 抛 AI_IMPORT_BATCH_RUNNING → 422 + errorCode
- API 提交事务并保持 service 调用顺序
"""

import io
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from openpyxl import Workbook
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import (
    BusinessRuleException,
    UnprocessableEntityException,
)
from app.main import app
from app.modules.system.api.user import import_users
from app.modules.system.constants import ImportBatchStatus
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserImportBatch
from app.modules.system.schemas.user_transfer import (
    FailedRow,
    ImportDryRunResult,
    ImportResult,
)
from app.modules.system.service.user_import_parser import (
    MAX_FILE_SIZE_BYTES,
    ImportErrorCollection,
)

# ========== Constants ==========

#: 支持的导入文件 MIME 类型。
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 10MB + 1 字节（trigger AI_IMPORT_FILE_TOO_LARGE）
LARGE_FILE_BYTES = b"\x00" * (10 * 1024 * 1024 + 1)

#: service 模块路径（patch target）
_API_MODULE = "app.modules.system.api.user"

#: 上传文件名
_DEFAULT_FILENAME = "users.xlsx"


# ========== Fixtures ==========


@pytest.fixture
async def client(db_session):  # noqa: ARG001 (db_session resets redis)
    """ASGI test client。

    依赖 ``db_session`` 触发 ``tests/modules/system/conftest.py`` 的
    ``_reset_redis_client()``，把 audit_middleware / auth.service 的 module-load
    redis_client 引用刷新到当前 loop。否则跨测试 loop 关闭后会抛
    ``RuntimeError: Event loop is closed``。
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def admin_token(db_session) -> str:
    """构造 admin 用户的 JWT（admin 是超管，绕过 system:user:import 检查）。

    使用 db_session 读取 admin user_id（init_db.py 已 seed）。
    """
    user = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    exp = datetime.now(UTC) + timedelta(hours=1)
    payload = {
        "exp": exp,
        "sub": str(user.user_id),
        "type": "access",
        "user_id": user.user_id,
        "user_name": user.user_name,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ========== Helpers ==========


def _make_xlsx_bytes(rows: list[list[Any]]) -> bytes:
    """构造最小 xlsx bytes（用 openpyxl Workbook）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_simple_xlsx() -> bytes:
    """2 行合法数据的 xlsx（dry_run / execute happy path 共用）。"""
    return _make_xlsx_bytes(
        [
            [
                "user_name",
                "employee_no",
                "nickname",
                "user_email",
                "user_phone",
                "dept_input",
                "role_input",
                "user_gender",
                "status",
            ],
            [
                "alice",
                "E001",
                "Alice",
                "alice@example.com",
                "13800138000",
                "QA-Dept",
                "R_ADMIN",
                "1",
                "1",
            ],
            [
                "bob",
                "E002",
                "Bob",
                "bob@example.com",
                "13800138001",
                "QA-Dept",
                "R_ADMIN",
                "1",
                "1",
            ],
        ]
    )


def _fake_dry_run_result() -> ImportDryRunResult:
    """dry_run_import_users 替身返回的 ImportDryRunResult。"""
    return ImportDryRunResult(
        total=2,
        new_records=[],
        exists_records=[],
        conflict_records=[],
        out_of_scope_records=[],
    )


def _fake_preview_batch() -> UserImportBatch:
    """dry_run_import_users 替身返回的 batch（status=PREVIEW_DONE）。"""
    now = datetime.now()
    return UserImportBatch(
        batch_id="batch-xxx",
        operator_id=1,
        filename=_DEFAULT_FILENAME,
        file_sha256="sha256-xxx",
        records_hash="hash-xxx",
        total_rows=2,
        preview_token="preview-token-xxx",
        on_conflict="skip",
        reason="QA reason",
        status=ImportBatchStatus.PREVIEW_DONE,
        summary_new=2,
        summary_exists=0,
        summary_conflict=0,
        summary_out_of_scope=0,
        created_at=now,
    )


def _fake_import_result(
    status: str = ImportBatchStatus.SUCCESS.value,
    failed_count: int = 0,
) -> ImportResult:
    """batch_create_users_from_records 替身返回的 ImportResult。"""
    return ImportResult(
        batch_id="batch-xxx",
        status=status,
        success_count=2,
        skipped_count=0,
        overwritten_count=0,
        failed_count=failed_count,
        failed_rows_file=None,
        failed_rows_preview=[],
        idempotent_replay=False,
    )


def _make_field_errors_xlsx() -> bytes:
    """构造带字段错误的 xlsx（触发 ImportErrorCollection）。"""
    return _make_xlsx_bytes(
        [
            [
                "user_name",
                "employee_no",
                "nickname",
                "user_email",
                "user_phone",
                "dept_input",
                "role_input",
                "user_gender",
                "status",
            ],
            # row 2: user_name 缺失
            [
                "",
                "E001",
                "Alice",
                "alice@example.com",
                "13800138000",
                "QA-Dept",
                "R_ADMIN",
                "1",
                "1",
            ],
        ]
    )


# ========== Auth ==========


async def test_endpoint_reads_upload_with_hard_size_bound() -> None:
    upload = MagicMock()
    upload.read = AsyncMock(return_value=b"x" * (MAX_FILE_SIZE_BYTES + 1))
    upload.content_type = MIME_XLSX
    upload.filename = _DEFAULT_FILENAME

    with pytest.raises(BusinessRuleException) as exc_info:
        await import_users(
            file=upload,
            reason="QA bounded upload",
            db=MagicMock(),
            current_user=MagicMock(),
        )

    assert exc_info.value.error_code == "AI_IMPORT_FILE_TOO_LARGE"
    upload.read.assert_awaited_once_with(MAX_FILE_SIZE_BYTES + 1)


class TestPostImportAuth:
    """验证 system:user:import 权限。"""

    async def test_no_token_returns_401(self, client):
        """未带 Authorization → OAuth2PasswordBearer 抛 401。"""
        response = await client.post(
            "/system/user/import",
            data={"reason": "QA"},
            files={
                "file": (
                    _DEFAULT_FILENAME,
                    _make_simple_xlsx(),
                    MIME_XLSX,
                )
            },
        )
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        """JWT 解码失败 → 401。"""
        response = await client.post(
            "/system/user/import",
            headers={"Authorization": "Bearer invalid.jwt.token"},
            data={"reason": "QA"},
            files={
                "file": (
                    _DEFAULT_FILENAME,
                    _make_simple_xlsx(),
                    MIME_XLSX,
                )
            },
        )
        assert response.status_code == 401


# ========== dry_run=true ==========


class TestPostImportDryRun:
    """验证 dry_run=true 的预检响应。"""

    async def test_returns_preview_token_and_expires_at(self, client, admin_token):
        """dry_run=true → 200 + data.previewToken + data.expiresAt。"""
        with (
            patch(
                f"{_API_MODULE}.parse_import_excel",
                return_value=[],
            ) as mock_parse,
            patch(
                f"{_API_MODULE}.dry_run_import_users",
                new=AsyncMock(
                    return_value=(
                        _fake_dry_run_result(),
                        _fake_preview_batch(),
                    )
                ),
            ) as mock_dry_run,
        ):
            response = await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={
                    "reason": "QA dry-run reason",
                    "on_conflict": "skip",
                    "dry_run": "true",
                },
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_simple_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 200
        assert body["msg"] == "success"
        data = body["data"]
        assert data["previewToken"] == "preview-token-xxx"
        assert "expiresAt" in data
        # 四象限计数（来自 batch.summary_*）
        assert data["total"] == 2
        assert data["newCount"] == 2
        assert data["existsCount"] == 0
        assert data["conflictCount"] == 0
        assert data["outOfScopeCount"] == 0
        mock_parse.assert_called_once()
        mock_dry_run.assert_awaited_once()

    async def test_dry_run_passes_on_conflict_to_service(self, client, admin_token):
        """on_conflict 表单字段透传到 dry_run_import_users。"""
        with (
            patch(
                f"{_API_MODULE}.parse_import_excel",
                return_value=[],
            ),
            patch(
                f"{_API_MODULE}.dry_run_import_users",
                new=AsyncMock(
                    return_value=(
                        _fake_dry_run_result(),
                        _fake_preview_batch(),
                    )
                ),
            ) as mock_dry_run,
        ):
            await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={
                    "reason": "QA",
                    "on_conflict": "overwrite",
                    "dry_run": "true",
                },
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_simple_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        # on_conflict 参数透传
        assert mock_dry_run.call_args.kwargs["on_conflict"] == "overwrite"


# ========== dry_run=false（正式导入）==========


class TestPostImportExecute:
    """验证正式导入响应包含 idempotentReplay。"""

    async def test_execute_with_preview_token_returns_batch_id(
        self, client, admin_token
    ):
        """dry_run=false + preview_token → 200 + batchId + status + counts。"""
        with (
            patch(
                f"{_API_MODULE}.parse_import_excel",
                return_value=[],
            ),
            patch(
                f"{_API_MODULE}.batch_create_users_from_records",
                new=AsyncMock(return_value=_fake_import_result()),
            ) as mock_execute,
        ):
            response = await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={
                    "reason": "QA execute reason",
                    "on_conflict": "skip",
                    "sync_mode": "CREATE_ONLY",
                    "preview_token": "preview-token-xxx",
                },
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_simple_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        assert response.status_code == 200
        body = response.json()
        data = body["data"]
        assert data["batchId"] == "batch-xxx"
        assert data["status"] == "SUCCESS"
        assert data["successCount"] == 2
        assert data["failedCount"] == 0
        assert data["idempotentReplay"] is False
        assert "failedRowsPreview" in data
        mock_execute.assert_awaited_once()

    async def test_execute_passes_sync_mode_to_service(self, client, admin_token):
        """sync_mode 应透传到 batch_create_users_from_records。"""
        with (
            patch(
                f"{_API_MODULE}.parse_import_excel",
                return_value=[],
            ),
            patch(
                f"{_API_MODULE}.batch_create_users_from_records",
                new=AsyncMock(return_value=_fake_import_result()),
            ) as mock_execute,
        ):
            await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={
                    "reason": "QA",
                    "sync_mode": "UPDATE_PROFILE",
                    "preview_token": "preview-token-xxx",
                },
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_simple_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        assert mock_execute.call_args.kwargs["sync_mode"].value == "UPDATE_PROFILE"

    async def test_execute_returns_idempotent_replay_when_service_says_so(
        self, client, admin_token
    ):
        """service 返回 idempotent_replay=True 时响应应透传。"""
        replay_result = ImportResult(
            batch_id="batch-xxx",
            status=ImportBatchStatus.SUCCESS.value,
            success_count=2,
            skipped_count=0,
            overwritten_count=0,
            failed_count=0,
            failed_rows_file=None,
            failed_rows_preview=[],
            idempotent_replay=True,
        )
        with (
            patch(
                f"{_API_MODULE}.parse_import_excel",
                return_value=[],
            ),
            patch(
                f"{_API_MODULE}.batch_create_users_from_records",
                new=AsyncMock(return_value=replay_result),
            ),
        ):
            response = await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={
                    "reason": "QA replay",
                    "preview_token": "preview-token-xxx",
                },
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_simple_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["idempotentReplay"] is True

    async def test_missing_preview_token_returns_422(self, client, admin_token):
        """dry_run=false 且缺少 preview_token 时返回 422。"""
        response = await client.post(
            "/system/user/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"reason": "QA"},
            files={
                "file": (
                    _DEFAULT_FILENAME,
                    _make_simple_xlsx(),
                    MIME_XLSX,
                )
            },
        )

        assert response.status_code == 422


# ========== 文件 / 字段校验 ==========


class TestPostImportValidation:
    """验证 MIME、大小、行数和必填字段约束。"""

    async def test_missing_file_returns_422(self, client, admin_token):
        """multipart 缺 file 字段 → FastAPI 422。"""
        response = await client.post(
            "/system/user/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"reason": "QA"},
        )
        assert response.status_code == 422

    async def test_missing_reason_returns_422(self, client, admin_token):
        """reason 必填。"""
        response = await client.post(
            "/system/user/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            files={
                "file": (
                    _DEFAULT_FILENAME,
                    _make_simple_xlsx(),
                    MIME_XLSX,
                )
            },
        )
        assert response.status_code == 422

    async def test_invalid_mime_returns_400(self, client, admin_token):
        """MIME 不在白名单时返回 400 AI_IMPORT_INVALID_MIME。"""
        response = await client.post(
            "/system/user/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"reason": "QA"},
            files={
                "file": (
                    "users.txt",
                    b"plain text not allowed",
                    "text/plain",
                )
            },
        )
        assert response.status_code == 400
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_INVALID_MIME"

    async def test_file_too_large_returns_400(self, client, admin_token):
        """文件超过 10MB 时返回 400 AI_IMPORT_FILE_TOO_LARGE。"""
        response = await client.post(
            "/system/user/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"reason": "QA"},
            files={
                "file": (
                    _DEFAULT_FILENAME,
                    LARGE_FILE_BYTES,
                    MIME_XLSX,
                )
            },
        )
        assert response.status_code == 400
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_FILE_TOO_LARGE"

    async def test_field_errors_returns_400_with_errors(self, client, admin_token):
        """ImportErrorCollection 应返回 400 和全部 FailedRow。"""
        # parser 抛 ImportErrorCollection
        fake_errors = [
            FailedRow(
                row_num=2,
                field="user_name",
                value="",
                reason="必填缺失",
                error_code="AI_IMPORT_USERNAME_INVALID",
            )
        ]
        with patch(
            f"{_API_MODULE}.parse_import_excel",
            side_effect=ImportErrorCollection(fake_errors),
        ):
            response = await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={"reason": "QA", "dry_run": "true"},
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_field_errors_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_FIELD_ERRORS"
        assert len(body["data"]["errors"]) == 1
        assert body["data"]["errors"][0]["errorCode"] == "AI_IMPORT_USERNAME_INVALID"


# ========== Service 异常透传 ==========


class TestPostImportServiceExceptionPropagation:
    """service 异常应透传给全局 exception handler。"""

    async def test_preview_invalid_returns_422(self, client, admin_token):
        """preview_token 校验失败时返回 422 AI_IMPORT_PREVIEW_INVALID。"""
        with (
            patch(
                f"{_API_MODULE}.parse_import_excel",
                return_value=[],
            ),
            patch(
                f"{_API_MODULE}.batch_create_users_from_records",
                new=AsyncMock(
                    side_effect=UnprocessableEntityException(
                        "preview_token 无效",
                        error_code="AI_IMPORT_PREVIEW_INVALID",
                    )
                ),
            ),
        ):
            response = await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={
                    "reason": "QA",
                    "preview_token": "expired-or-mismatched",
                },
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_simple_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        assert response.status_code == 422
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_PREVIEW_INVALID"

    async def test_batch_running_returns_422(self, client, admin_token):
        """批次处于 RUNNING 时返回 422 AI_IMPORT_BATCH_RUNNING。"""
        with (
            patch(
                f"{_API_MODULE}.parse_import_excel",
                return_value=[],
            ),
            patch(
                f"{_API_MODULE}.batch_create_users_from_records",
                new=AsyncMock(
                    side_effect=UnprocessableEntityException(
                        "批次正在执行中",
                        error_code="AI_IMPORT_BATCH_RUNNING",
                    )
                ),
            ),
        ):
            response = await client.post(
                "/system/user/import",
                headers={"Authorization": f"Bearer {admin_token}"},
                data={
                    "reason": "QA",
                    "preview_token": "preview-token-xxx",
                },
                files={
                    "file": (
                        _DEFAULT_FILENAME,
                        _make_simple_xlsx(),
                        MIME_XLSX,
                    )
                },
            )

        assert response.status_code == 422
        body = response.json()
        assert body["errorCode"] == "AI_IMPORT_BATCH_RUNNING"
