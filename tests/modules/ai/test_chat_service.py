"""chat_service.build_chat_deps 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §4.6 / §17.2。

验证 ChatDeps 完整字段构造（user / perms / db / data_scope / agent / trace_id），
超管 / 普通 user 两条路径，agent_code 不存在时抛错。
"""

# ruff: noqa: ARG001, ARG005, PLC0415  test 占位参数 + 局部 monkeypatch import

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.core.context import ChatDeps
from app.modules.ai.service.chat_service import chat_service


class TestBuildChatDeps:
    """超管路径：data_scope=None / agent=从 DB 加载 / trace_id 自动生成"""

    async def test_super_admin_returns_all_visible_data_scope(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)

        mock_user = MagicMock()
        mock_user.user_id = 999
        mock_user.roles = []
        mock_user.depts = []

        deps = await chat_service.build_chat_deps(db_session, mock_user)

        assert isinstance(deps, ChatDeps)
        assert deps.user is mock_user
        assert deps.db is db_session
        # 超管 data_scope 全部可见
        assert deps.data_scope.accessible_dept_ids is None
        assert deps.data_scope.accessible_user_ids is None
        assert deps.data_scope.filters == []

    async def test_trace_id_auto_generated_format(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spec §4.6: trace_id 默认 tr_<uuid hex>"""
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)
        mock_user = MagicMock()
        mock_user.roles = []
        mock_user.depts = []

        deps = await chat_service.build_chat_deps(db_session, mock_user)

        assert deps.trace_id.startswith("tr_")
        assert len(deps.trace_id) >= 19  # tr_ + 16 hex chars

    async def test_trace_id_explicit_passthrough(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """调用方传入 trace_id 时复用（如重试场景）"""
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)
        mock_user = MagicMock()
        mock_user.roles = []
        mock_user.depts = []

        deps = await chat_service.build_chat_deps(
            db_session, mock_user, trace_id="tr_custom_abc"
        )
        assert deps.trace_id == "tr_custom_abc"

    async def test_agent_code_not_found_raises(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spec §4.6: agent_code 必须在 ai_agent 表存在"""
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)
        mock_user = MagicMock()
        mock_user.roles = []
        mock_user.depts = []

        with pytest.raises(ValueError, match="not found in ai_agent table"):
            await chat_service.build_chat_deps(
                db_session, mock_user, agent_code="missing_agent"
            )

    async def test_default_agent_code_is_user_mgmt(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spec §10.1: MVP 默认 user_mgmt Agent"""
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)
        mock_user = MagicMock()
        mock_user.roles = []
        mock_user.depts = []

        deps = await chat_service.build_chat_deps(db_session, mock_user)
        assert deps.agent.code == "user_mgmt"
        assert deps.agent.is_builtin is True


class TestAttachTraceToConversation:
    """spec §4.5: trace_id + agent_code 写到 ai_conversation"""

    async def test_none_conversation_id_skips(self, db_session: AsyncSession) -> None:
        """conversation_id=None 时跳过（不报错）"""
        await chat_service.attach_trace_to_conversation(
            db_session, None, "user_mgmt", "tr_abc"
        )
        # 无异常即通过

    async def test_nonexistent_conversation_skips(
        self, db_session: AsyncSession
    ) -> None:
        """conversation_id 不存在时不报错（防御性）"""
        await chat_service.attach_trace_to_conversation(
            db_session, 99999999, "user_mgmt", "tr_abc"
        )
