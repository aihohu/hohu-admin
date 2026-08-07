"""chat_service.build_chat_deps 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §4.6 / §17.2。

验证 ChatDeps 完整字段构造（user / perms / db / data_scope / agent / trace_id），
超管 / 普通 user 两条路径，agent_code 不存在时抛错。

注：Task 11 后 build_chat_deps 内部调 resolve_sticky_agent_code；本文件单测
patch 该函数返回 manual_override，聚焦测 build_chat_deps 自身字段构造逻辑
（stickiness 决策树由 test_session_stickiness.py 覆盖）.
"""

# ruff: noqa: ARG001, ARG005, PLC0415  test 占位参数 + 局部 monkeypatch import

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.core.context import ChatDeps
from app.modules.ai.service.chat_service import chat_service


def _patch_sticky_manual(agent_code: str = "user_mgmt"):
    """Patch resolve_sticky_agent_code 返回 manual_override，绕过决策树.

    build_chat_deps 内部 inline import 调用，所以 patch 源模块即可.
    """
    from app.modules.ai.agents.supervisor.stickiness import StickyDecision

    return patch(
        "app.modules.ai.agents.supervisor.stickiness.resolve_sticky_agent_code",
        AsyncMock(
            return_value=StickyDecision(
                agent_code=agent_code,
                run_supervisor=False,
                reason="manual_override",
            )
        ),
    )


class TestBuildChatDeps:
    """超管路径：data_scope=None / agent=从 DB 加载 / trace_id 自动生成"""

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    async def test_super_admin_returns_all_visible_data_scope(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)

        mock_user = MagicMock()
        mock_user.user_id = 999
        mock_user.roles = []
        mock_user.depts = []

        with _patch_sticky_manual():
            deps = await chat_service.build_chat_deps(db_session, mock_user)

        assert isinstance(deps, ChatDeps)
        assert deps.user is mock_user
        assert deps.db is db_session
        # 超管 data_scope 全部可见
        assert deps.data_scope.accessible_dept_ids is None
        assert deps.data_scope.accessible_user_scope is None
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

        with _patch_sticky_manual():
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

        with _patch_sticky_manual():
            deps = await chat_service.build_chat_deps(
                db_session, mock_user, trace_id="tr_custom_abc"
            )
        assert deps.trace_id == "tr_custom_abc"

    async def test_tenant_id_comes_from_server_resolver(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """客户端字段不参与 tenant 解析；ChatDeps 只接收服务端 resolver 结果。"""
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)
        mock_user = MagicMock()
        mock_user.roles = []
        mock_user.depts = []

        with (
            _patch_sticky_manual(),
            patch(
                "app.modules.ai.service.chat_service.resolve_tenant_id",
                return_value=37,
            ) as resolver,
        ):
            deps = await chat_service.build_chat_deps(db_session, mock_user)

        resolver.assert_called_once_with(mock_user)
        assert deps.tenant_id == 37

    async def test_agent_code_not_found_raises(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spec §4.6: agent_code 必须在 ai_agent 表存在"""
        import app.modules.ai.core.data_scope_loader as loader_mod

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)
        mock_user = MagicMock()
        mock_user.roles = []
        mock_user.depts = []

        with _patch_sticky_manual("missing_agent"):
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

        with _patch_sticky_manual():
            deps = await chat_service.build_chat_deps(db_session, mock_user)
        assert deps.agent.code == "user_mgmt"
        assert deps.agent.is_builtin is True

    async def test_super_admin_bypass_perm_filter(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超管 bypass AI tool 网关 perm 过滤（与 HTTP API 层 require_permissions 对齐）.

        背景：HTTP API 层 require_permissions 用 is_super_admin bypass，admin 调
        任何 endpoint 都通过。AI tool 网关原本用 collect_user_buttons 收集 perm，
        没给超管 bypass → admin 调 role.list 等 tool 时 LLM 看不到，会幻觉"工具不存在"。
        修复：build_chat_deps 检测 is_super_admin 后用 all_registry_perms() 替代.
        """
        import app.modules.ai.agents.tools.file_tools  # noqa: F401
        import app.modules.ai.core.data_scope_loader as loader_mod
        import app.modules.job.ai_tools  # noqa: F401

        # 触发 tool 注册
        import app.modules.system.ai_tools  # noqa: F401
        from app.modules.ai.agents.tools.registry import ToolRegistry

        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)
        mock_user = MagicMock()
        mock_user.user_name = "admin"
        mock_user.roles = []  # 故意空：没绑任何 menu，验证 bypass 工作
        mock_user.depts = []

        with _patch_sticky_manual():
            deps = await chat_service.build_chat_deps(db_session, mock_user)

        # 期望：超管 perms 覆盖 registry 中所有 tool 的 required_perms 并集
        all_required_perms: set[str] = set()
        for t in ToolRegistry.get().all():
            all_required_perms.update(t.meta.required_perms)
        assert all_required_perms, "registry should have tools"
        # 用system:role:list / system:dept:list 这两个之前缺的 perm 当代表断言
        assert "system:role:list" in deps.perms
        assert "system:dept:list" in deps.perms
        assert all_required_perms <= deps.perms


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


async def _add_user(db: AsyncSession, *, user_id: int, user_name: str) -> None:
    """建用户（满足 ai_conversation.user_id 外键）"""
    from app.modules.system.models.user import User  # noqa: PLC0415

    db.add(
        User(
            user_id=user_id,
            user_name=user_name,
            nickname=user_name,
            hashed_password="$2b$12$dummy",
            status="1",
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_save_user_message_writes_agent_code(db_session):
    """spec §4.1 step 5: save_user_message 透传 agent_code 到 ai_message.agent_code."""
    from sqlalchemy import select

    from app.core.id_generator import next_id
    from app.modules.ai.models.conversation import AiConversation
    from app.modules.ai.models.message import AiMessage
    from app.modules.ai.service.chat_service import chat_service

    await _add_user(db_session, user_id=9001, user_name="ai_test_u1")
    conv = AiConversation(
        conversation_id=next_id(),
        user_id=9001,
        title="test",
    )
    db_session.add(conv)
    await db_session.flush()

    await chat_service.save_user_message(
        db_session,
        conv.conversation_id,
        9001,
        "hello",
        agent_code="user_mgmt",
    )
    await db_session.flush()

    msg = (
        await db_session.execute(
            select(AiMessage).where(
                AiMessage.conversation_id == conv.conversation_id,
                AiMessage.role == "user",
            )
        )
    ).scalar_one()
    assert msg.agent_code == "user_mgmt"


@pytest.mark.asyncio
async def test_save_assistant_message_writes_agent_code(db_session):
    """spec §4.1 step 5: save_assistant_message 透传 agent_code."""
    from sqlalchemy import select

    from app.core.id_generator import next_id
    from app.modules.ai.models.conversation import AiConversation
    from app.modules.ai.models.message import AiMessage
    from app.modules.ai.service.chat_service import chat_service

    await _add_user(db_session, user_id=9002, user_name="ai_test_u2")
    conv = AiConversation(conversation_id=next_id(), user_id=9002, title="t")
    db_session.add(conv)
    await db_session.flush()

    await chat_service.save_assistant_message(
        db_session,
        conv.conversation_id,
        content="hi",
        agent_code="role_mgmt",
    )
    await db_session.flush()

    msg = (
        await db_session.execute(
            select(AiMessage).where(
                AiMessage.conversation_id == conv.conversation_id,
                AiMessage.role == "assistant",
            )
        )
    ).scalar_one()
    assert msg.agent_code == "role_mgmt"
