"""chat_service.build_chat_deps 单元测试

覆盖 ChatDeps 构造、Agent 加载和消息持久化。

验证 ChatDeps 完整字段构造（user / perms / db / data_scope / agent / trace_id），
超管 / 普通 user 两条路径，agent_code 不存在时抛错。

build_chat_deps 内部调用 resolve_sticky_agent_code；本文件单测
patch 该函数返回 manual_override，聚焦测 build_chat_deps 自身字段构造逻辑
（stickiness 决策树由 test_session_stickiness.py 覆盖）.
"""

# ruff: noqa: ARG001, ARG005, PLC0415  test 占位参数 + 局部 monkeypatch import

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.core.context import ChatDeps
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.system.models.user import User
from app.utils.data_scope import DataScopeResolution


def _principal() -> User:
    return User(
        user_id=999,
        tenant_id=0,
        user_name="chat-service-test",
        roles=[],
        depts=[],
    )


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


def _patch_unbounded_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the shared resolver for tests focused on ChatDeps assembly."""
    import app.modules.ai.core.data_scope_loader as loader_mod

    monkeypatch.setattr(
        loader_mod,
        "resolve_data_scope",
        AsyncMock(
            return_value=DataScopeResolution(
                scope_kinds=frozenset({"1"}),
                accessible_dept_ids=None,
                accessible_user_scope=None,
                include_self=True,
                unbounded=True,
            )
        ),
    )


@pytest.fixture
async def authorized_agent_policy(db_session: AsyncSession):
    """本文件只测 deps 组装；Agent Policy 自身由独立测试覆盖。"""
    agent = await db_session.scalar(select(AiAgent).where(AiAgent.code == "user_mgmt"))
    assert agent is not None
    agent.enabled = True
    with (
        patch.object(
            agent_authorization_service,
            "authorize_agent_access",
            AsyncMock(return_value=agent),
        ),
        patch.object(
            agent_authorization_service,
            "tool_permissions",
            MagicMock(
                side_effect=lambda _user: {
                    permission
                    for tool in ToolRegistry.get().all()
                    for permission in tool.meta.required_perms
                }
            ),
        ),
    ):
        yield


@pytest.mark.usefixtures("authorized_agent_policy")
class TestBuildChatDeps:
    """超管路径：data_scope=None / agent=从 DB 加载 / trace_id 自动生成"""

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    async def test_super_admin_returns_all_visible_data_scope(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_unbounded_scope(monkeypatch)

        mock_user = _principal()

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
        """trace_id 默认格式为 tr_<uuid hex>。"""
        _patch_unbounded_scope(monkeypatch)
        mock_user = _principal()

        with _patch_sticky_manual():
            deps = await chat_service.build_chat_deps(db_session, mock_user)

        assert deps.trace_id.startswith("tr_")
        assert len(deps.trace_id) >= 19  # tr_ + 16 hex chars

    async def test_trace_id_explicit_passthrough(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """调用方传入 trace_id 时复用（如重试场景）"""
        _patch_unbounded_scope(monkeypatch)
        mock_user = _principal()

        with _patch_sticky_manual():
            deps = await chat_service.build_chat_deps(
                db_session, mock_user, trace_id="tr_custom_abc"
            )
        assert deps.trace_id == "tr_custom_abc"

    async def test_tenant_id_comes_from_server_resolver(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """客户端字段不参与 tenant 解析；ChatDeps 只接收服务端 resolver 结果。"""
        _patch_unbounded_scope(monkeypatch)
        mock_user = _principal()

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
        """agent_code 必须在 ai_agent 表中存在。"""
        _patch_unbounded_scope(monkeypatch)
        mock_user = _principal()

        denied = AuthorizationException(
            "当前用户不可使用该 AI Agent",
            error_code="AI_AGENT_FORBIDDEN",
        )
        with (
            _patch_sticky_manual("missing_agent"),
            patch.object(
                agent_authorization_service,
                "authorize_agent_access",
                AsyncMock(side_effect=denied),
            ),
        ):
            with pytest.raises(AuthorizationException) as exc_info:
                await chat_service.build_chat_deps(
                    db_session, mock_user, agent_code="missing_agent"
                )
        assert exc_info.value.error_code == "AI_AGENT_FORBIDDEN"

    async def test_default_agent_code_is_user_mgmt(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认使用 user_mgmt Agent。"""
        _patch_unbounded_scope(monkeypatch)
        mock_user = _principal()

        with _patch_sticky_manual():
            deps = await chat_service.build_chat_deps(db_session, mock_user)
        assert deps.agent.code == "user_mgmt"
        assert deps.agent.is_builtin is True


async def test_revoked_sticky_agent_is_cleared_and_rerouted(
    db_session: AsyncSession,
) -> None:
    """旧会话粘滞失权后不沿用，交回最新候选集路由。"""
    from app.core.id_generator import next_id
    from app.modules.system.models.user import User

    user = await db_session.scalar(select(User).where(User.user_name == "admin"))
    conversation = AiConversation(
        conversation_id=next_id(),
        user_id=user.user_id,
        title="sticky revoked",
        agent_code="role_mgmt",
    )
    db_session.add(conversation)
    await db_session.flush()
    denied = AuthorizationException(
        "当前用户不可使用该 AI Agent",
        error_code="AI_AGENT_FORBIDDEN",
    )

    with patch.object(
        agent_authorization_service,
        "authorize_agent_access",
        AsyncMock(side_effect=denied),
    ):
        deps = await chat_service.build_chat_deps(
            db_session,
            user,
            conversation_id=conversation.conversation_id,
        )

    assert conversation.agent_code is None
    assert deps.agent is None
    assert deps.sticky_decision.run_supervisor is True
    assert deps.sticky_decision.reason == "auto_fallback_forbidden"


async def test_create_agent_preserves_explicit_falsy_model_ref() -> None:
    """Service 不能把显式空 model ref 替换成 Agent preference。"""
    db = MagicMock()
    selected_model = MagicMock(name="selected_model")
    selector = AsyncMock(return_value=selected_model)
    built_agent = MagicMock(name="built_agent")

    with (
        patch.object(
            model_authorization_service,
            "resolve_model_instance",
            selector,
        ),
        patch(
            "app.modules.ai.agents.safety.ai_config.get_ai_config_str_list",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.modules.ai.service.chat_service.create_chat_agent",
            return_value=built_agent,
        ),
    ):
        result = await chat_service.create_agent(
            db,
            "",
            user_perms=set(),
            agent_config=SimpleNamespace(model_preference="provider:preferred"),
        )

    assert result is built_agent
    selector.assert_awaited_once_with(db, "", tenant_id=0)


class TestAttachTraceToConversation:
    """trace_id 和 agent_code 写入 ai_conversation。"""

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
    """save_user_message 将 agent_code 写入 ai_message.agent_code。"""
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
    """save_assistant_message 透传 agent_code。"""
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
