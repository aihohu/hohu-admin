"""POST/GET /system/user/export HTTP 契约测试（Task 13，spec §5.2 + §2.31 P1-5）。

只验证 HTTP 契约层（路由 / JSON body / streaming 响应 / 错误码），
service 层（export_users_to_excel / get_export_task / list_export_tasks）
用 patch 替身。完整业务流程在 test_user_export.py 已覆盖。

覆盖：
- 401 未登录 / 无效 JWT（auth gating）
- POST /export → 200 StreamingResponse xlsx + Content-Disposition（spec §5.2 line 2200）
- POST /export 透传 filter + reason 给 service
- POST /export 缺 reason → 422（Pydantic 校验）
- POST /export 全空白 reason → 422（spec §2.30）
- POST /export service 抛 AI_EXPORT_ASYNC_REQUIRED → 422（spec §5.2 line 2201）
- GET /export/{id} → 200 + UserExportTaskResponse（spec §2.31 line 1589-1591）
- GET /export/{id} 不存在 → 404 + AI_EXPORT_TASK_NOT_FOUND
- GET /export → 分页列表（spec §2.31 line 1593-1595）
- 调用方 commit + service 调用顺序（spec §3.6）
"""

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import select

from app.core.base_response import PageResult
from app.core.config import settings
from app.core.exceptions import UnprocessableEntityException
from app.main import app
from app.modules.system.models.user import User
from app.modules.system.user.constants import ExportTaskStatus
from app.modules.system.user.schemas import UserExportTaskResponse

# ========== Constants ==========

#: spec §2.9 / §5.2：xlsx MIME
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: xlsx 文件 magic bytes（PK zip header），用于校验 streaming body 是真实 xlsx
_XLSX_MAGIC = b"\x50\x4b\x03\x04"

#: service 模块路径（patch target）
_API_MODULE = "app.modules.system.api.user"


# ========== Fixtures ==========


@pytest.fixture
async def client(db_session):  # noqa: ARG001 (db_session resets redis)
    """ASGI test client。

    依赖 ``db_session`` 触发 ``tests/modules/system/conftest.py`` 的
    ``_reset_redis_client()``，刷新 audit_middleware / auth.service 的 redis_client
    绑定到当前 loop（与 test_user_import_api.py 同款）。
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def admin_token(db_session) -> str:
    """admin 用户 JWT（admin 绕过 system:user:export 检查）。"""
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


def _fake_xlsx_bytes() -> bytes:
    """最小 xlsx bytes（PK 头 + 一些内容，足够 streaming 测试）。"""
    return _XLSX_MAGIC + b"\x00" * 100


def _fake_export_task_response(
    export_id: str = "exp-xxx",
    status: ExportTaskStatus = ExportTaskStatus.SUCCESS,
) -> UserExportTaskResponse:
    """GET /export/{id} 替身返回值（spec §3.6 UserExportTaskResponse）。"""
    return UserExportTaskResponse(
        export_id=export_id,
        operator_id=1,
        filter_snapshot={"user_name": "alice"},
        reason="QA export reason",
        row_count=10,
        file_storage_key="user-export/abc.xlsx",
        file_size_bytes=1024,
        status=status.value,
        created_at=datetime(2026, 8, 4, 10, 0, 0),
        started_at=datetime(2026, 8, 4, 10, 0, 1),
        finished_at=datetime(2026, 8, 4, 10, 0, 2),
        duration_ms=1000,
    )


# ========== Auth ==========


class TestPostExportAuth:
    """spec §5.2 line 2196：权限 system:user:export。"""

    async def test_no_token_returns_401(self, client):
        response = await client.post(
            "/system/user/export",
            json={"reason": "QA"},
        )
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        response = await client.post(
            "/system/user/export",
            headers={"Authorization": "Bearer invalid.jwt.token"},
            json={"reason": "QA"},
        )
        assert response.status_code == 401


# ========== POST /export 同步路径 ==========


class TestPostExportSync:
    """spec §5.2 line 2200：同步路径返回 StreamingResponse xlsx。"""

    async def test_returns_streaming_xlsx_with_content_disposition(
        self, client, admin_token
    ):
        """200 + Content-Disposition: attachment; filename=users_YYYYMMDD.xlsx。"""
        with patch(
            f"{_API_MODULE}.export_users_to_excel",
            new=AsyncMock(return_value=(_fake_xlsx_bytes(), 10, "exp-xxx")),
        ) as mock_service:
            response = await client.post(
                "/system/user/export",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "userName": "alice",
                    "deptId": "1",
                    "status": "1",
                    "reason": "QA export reason",
                },
            )

        assert response.status_code == 200, response.text
        # Content-Type 是 xlsx MIME
        assert response.headers["content-type"] == XLSX_MIME
        # Content-Disposition 格式：attachment; filename=users_YYYYMMDD.xlsx
        cd = response.headers.get("content-disposition", "")
        assert "attachment" in cd, f"Content-Disposition 缺 attachment: {cd}"
        # 8 位日期（今日）
        match = re.search(r"filename=users_(\d{8})\.xlsx", cd)
        assert match is not None, f"filename 不符合 spec: {cd}"
        # body 是 xlsx bytes（PK magic header）
        assert response.content.startswith(_XLSX_MAGIC)
        mock_service.assert_awaited_once()

    async def test_passes_filter_and_reason_to_service(self, client, admin_token):
        """filter + reason 透传到 export_users_to_excel（spec §5.2 line 2189-2194）。"""
        with patch(
            f"{_API_MODULE}.export_users_to_excel",
            new=AsyncMock(return_value=(_fake_xlsx_bytes(), 5, "exp-xxx")),
        ) as mock_service:
            await client.post(
                "/system/user/export",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "userName": "bob",
                    "status": "1",
                    "reason": "月度归档",
                },
            )

        # export_users_to_excel(db, filter_, current_user, *, reason)
        # filter_ 是位置参数 args[1]，reason 是 kwargs
        filter_arg = mock_service.call_args.args[1]
        assert filter_arg.user_name == "bob"
        assert filter_arg.status == "1"
        assert mock_service.call_args.kwargs["reason"] == "月度归档"


# ========== POST /export 校验 ==========


class TestPostExportValidation:
    """spec §2.30 v2.2 P1-3：reason 必填，1-256 字符。"""

    async def test_missing_reason_returns_422(self, client, admin_token):
        """spec §2.30：reason 必填。"""
        response = await client.post(
            "/system/user/export",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"userName": "alice"},
        )
        assert response.status_code == 422

    async def test_empty_reason_returns_422(self, client, admin_token):
        """spec §2.30：全空白 reason 拒绝（ReasonSchema._strip_and_require_non_empty）。"""
        response = await client.post(
            "/system/user/export",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"userName": "alice", "reason": "   "},
        )
        assert response.status_code == 422


# ========== POST /export 异步阈值 ==========


class TestPostExportAsyncRequired:
    """spec §5.2 line 2201 + §2.6：行数 > 5000 → 422 AI_EXPORT_ASYNC_REQUIRED。"""

    async def test_async_required_returns_422(self, client, admin_token):
        """service 抛 UnprocessableEntityException(AI_EXPORT_ASYNC_REQUIRED)
        → API 层透传给全局 handler，HTTP 422。
        """
        with patch(
            f"{_API_MODULE}.export_users_to_excel",
            new=AsyncMock(
                side_effect=UnprocessableEntityException(
                    "导出行数 6000 超过同步阈值 5000，请等待异步通道开放",
                    error_code="AI_EXPORT_ASYNC_REQUIRED",
                )
            ),
        ):
            response = await client.post(
                "/system/user/export",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"reason": "QA"},
            )

        assert response.status_code == 422
        body = response.json()
        assert body["errorCode"] == "AI_EXPORT_ASYNC_REQUIRED"


# ========== GET /export/{export_id} ==========


class TestGetExportDetail:
    """spec §2.31 line 1589-1591：GET /export/{export_id} 任务详情。"""

    async def test_no_token_returns_401(self, client):
        response = await client.get("/system/user/export/exp-xxx")
        assert response.status_code == 401

    async def test_returns_task_details(self, client, admin_token):
        fake_task = _fake_export_task_response()
        with patch(
            f"{_API_MODULE}.get_export_task",
            new=AsyncMock(return_value=fake_task),
        ):
            response = await client.get(
                "/system/user/export/exp-xxx",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 200
        data = body["data"]
        assert data["exportId"] == "exp-xxx"
        assert data["status"] == "SUCCESS"
        assert data["rowCount"] == 10
        assert data["reason"] == "QA export reason"
        # operator_id 字符串化（防 JS BigInt 精度丢失）
        assert data["operatorId"] == "1"
        assert isinstance(data["operatorId"], str)

    async def test_not_found_returns_404(self, client, admin_token):
        with patch(
            f"{_API_MODULE}.get_export_task",
            new=AsyncMock(return_value=None),
        ):
            response = await client.get(
                "/system/user/export/nonexistent",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 404
        body = response.json()
        assert body["errorCode"] == "AI_EXPORT_TASK_NOT_FOUND"


# ========== GET /export（列表） ==========


class TestGetExportList:
    """spec §2.31 line 1593-1595：GET /export 分页列表。"""

    async def test_no_token_returns_401(self, client):
        response = await client.get("/system/user/export")
        assert response.status_code == 401

    async def test_returns_paginated_list(self, client, admin_token):
        records = [
            _fake_export_task_response(export_id="e1"),
            _fake_export_task_response(export_id="e2"),
        ]
        fake_page = PageResult(records=records, total=2, current=1, size=10)
        with patch(
            f"{_API_MODULE}.list_export_tasks",
            new=AsyncMock(return_value=fake_page),
        ) as mock_list:
            response = await client.get(
                "/system/user/export?current=1&size=10",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        data = body["data"]
        assert data["total"] == 2
        assert data["current"] == 1
        assert data["size"] == 10
        assert len(data["records"]) == 2
        assert data["records"][0]["exportId"] == "e1"
        # query 透传到 service
        query_arg = mock_list.call_args.args[1]
        assert query_arg.current == 1
        assert query_arg.size == 10

    async def test_passes_filters_to_service(self, client, admin_token):
        """status / operator_id 过滤参数透传到 list_export_tasks。"""
        fake_page = PageResult(records=[], total=0, current=1, size=10)
        with patch(
            f"{_API_MODULE}.list_export_tasks",
            new=AsyncMock(return_value=fake_page),
        ) as mock_list:
            await client.get(
                "/system/user/export?current=2&size=20&status=SUCCESS&operatorId=1",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        query_arg = mock_list.call_args.args[1]
        assert query_arg.current == 2
        assert query_arg.size == 20
        assert query_arg.status == "SUCCESS"
        assert query_arg.operator_id == 1
