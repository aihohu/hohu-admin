"""AI tool `user.export` 单测（Task 33，spec §2.31 line 1626 落地）。

Task 27 已实现同步导出 + 强制建 ExportTask；Task 33 把 result_view 从
rows_affected 升级为 detail_card，携带 downloadUrl / fileSize / expiresAt，
让前端 DetailCardView 渲染「下载」按钮（AI 对话内闭环）。

覆盖：
- result_view == "detail_card"
- view_data 含 downloadUrl / fileSize / expiresAt / rowCount / exportId
- data（LLM 视角）含 exportId + rowCount + downloadUrl
- downloadUrl 路径与 GET /export/{export_id}/download 端点一致
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.file_storage import MockFileStorage
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.ai_tools import user_export


def _make_ctx(db: AsyncSession) -> AiToolContext:
    """最小 AiToolContext：超管视角（无 data_scope filter）。"""
    data_scope = DataScopeContext(
        accessible_dept_ids=None,
        accessible_user_scope=None,
        filters=[],
    )
    meta = AiToolMeta(
        name="user.export",
        agent="user_mgmt",
        summary="导出用户",
        risk="low",
        required_perms=("system:user:export",),
        allowed_filters=("status",),
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms={"system:user:export"},
        db=db,
        data_scope=data_scope,
        trace_id="tr_export_test",
        tool_meta=meta,
    )


@pytest.fixture
def file_storage() -> MockFileStorage:
    return MockFileStorage()


class TestUserExportDetailCard:
    """Task 33：user_export 返回 detail_card + downloadUrl。"""

    async def test_result_view_is_detail_card(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """result_view 从 rows_affected → detail_card（spec line 2619）。"""
        # Mock get_file_storage 让 export_users_to_excel 用 MockFileStorage
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export detail card")

        assert result.ui is not None
        assert result.ui.view_type == "detail_card"

    async def test_view_data_contains_download_url(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """view_data 含 downloadUrl 指向 GET /export/{export_id}/download。"""
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export download url")

        view_data = result.ui.view_data
        assert "downloadUrl" in view_data
        export_id = result.data["exportId"]
        # 路径模板：/system/user/export/{export_id}/download
        assert view_data["downloadUrl"] == f"/system/user/export/{export_id}/download"

    async def test_view_data_contains_file_size_and_expiry(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """view_data 含 fileSize / expiresAt（spec line 1626）。"""
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export metadata")

        view_data = result.ui.view_data
        assert "fileSize" in view_data
        assert isinstance(view_data["fileSize"], int)
        assert view_data["fileSize"] > 0
        assert "expiresAt" in view_data
        # ISO 8601 字符串
        assert isinstance(view_data["expiresAt"], str)
        assert "T" in view_data["expiresAt"]

    async def test_data_carries_export_id_and_download_url(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """data（LLM 视角）含 exportId + rowCount + downloadUrl。

        LLM 引导用户点击下载时需要 downloadUrl 字面量；
        rowCount 已在 data 中（Task 27 现状），保持。
        """
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export data")

        assert "exportId" in result.data
        assert "rowCount" in result.data
        assert "downloadUrl" in result.data
        assert result.data["downloadUrl"].endswith("/download")
