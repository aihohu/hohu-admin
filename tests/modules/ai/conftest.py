"""ai 模块测试共用 fixture。

db_session 用 SQLAlchemy 官方推荐的「outer transaction + 强制 rollback」模式：
session 绑定到一个手动管理的外层事务；session.commit() 只提交 savepoint，不
影响外层事务；fixture 退出时强制 rollback 外层事务，所有写入撤销，绝不落库。

历史上的实现曾用 `DELETE FROM sys_user` 让 count/distinct 测试避开历史数据，
但该 DELETE 会通过测试代码中的隐式 commit 真正落库，曾造成生产 sys_user 被清空
事故。当前实现彻底移除所有 DELETE / TRUNCATE 操作 —— 测试代码可以放心 commit，
outer rollback 兜底。
"""

# ruff: noqa: ARG001, PLC0415, UP017  test fixture 占位参数 + 函数内 import（避免顶层副作用）

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.db.session import AsyncSessionLocal, engine


def _reset_redis_client() -> None:
    """每个测试新建事件循环时，重建 redis 客户端绑定到当前 loop。

    同步刷新 gateway.executor 内的 redis_client 引用（顶部 import 时绑定，
    不会跟着 redis_module.redis_client 重新解析）。
    """
    redis_module.redis_pool = aioredis.ConnectionPool.from_url(
        redis_module.settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    redis_module.redis_client = aioredis.Redis(connection_pool=redis_module.redis_pool)

    # 刷新 gateway.executor 内引用
    from app.modules.ai.agents.gateway import executor as exec_mod  # noqa: PLC0415

    exec_mod.redis_client = redis_module.redis_client

    # Task 11+: 刷新 auth.service.redis_client 引用 —— /ai/chat 经 get_current_user
    # dependency 调 _is_blacklisted（redis_client.get token blacklist）。若不刷新，
    # 跨测试 loop 关闭后此引用仍指向上轮 loop 的客户端，触发 "Event loop is closed".
    from app.modules.auth import service as auth_service  # noqa: PLC0415

    auth_service.redis_client = redis_module.redis_client

    # Task 11+: chat.py 顶部 `from app.core.redis import redis_client` 也绑死引用，
    # is_ip_blacklisted / record_injection_hit_conversation 用的是这个引用.
    from app.modules.ai.api import chat as chat_mod  # noqa: PLC0415

    chat_mod.redis_client = redis_module.redis_client

    # Task 11+: supervisor.quota 模块也顶部 import redis_client（spec §9 配额检查）.
    from app.modules.ai.agents.supervisor import quota as quota_mod  # noqa: PLC0415

    quota_mod.redis_client = redis_module.redis_client

    # Task 12+: audit_middleware.py 顶部 `from app.core.redis import redis_client` 也
    # 绑死引用（_resolve_username 走 redis 缓存）。每个请求都过 middleware，loop
    # 切换后若不刷新，会触发 "Event loop is closed" 让整个请求 500.
    from app.middleware import audit_middleware as audit_mod  # noqa: PLC0415

    audit_mod.redis_client = redis_module.redis_client


@pytest.fixture
async def db_session() -> AsyncSession:
    """每个测试独立 session，结束后强制 rollback 外层事务（绝不落库）。

    实现要点：
    - 用 engine.connect() 拿独立 connection（不进连接池常规路径）
    - 手动 conn.begin() 开 outer transaction
    - session 绑到这个 connection，session.commit() 只 commit savepoint
    - finally 块强制 outer.rollback()，无论测试通过 / 抛异常都撤销
    - teardown 阶段顺手精准清理 ai_operation_log 测试残留（trace_id LIKE 'tr_test%'）；
      executor.py 内部用独立 AsyncSessionLocal() 写日志绕过了 outer transaction，
      必须靠这里兜底。不是 TRUNCATE，只删测试自己写的行。

    本 fixture 不做任何 DELETE FROM <table>（无 WHERE）/ TRUNCATE。
    """
    from sqlalchemy import text  # noqa: PLC0415

    _reset_redis_client()
    # 入口 dispose：上个测试 loop 关闭后，连接池里残留的 asyncpg connection
    # 仍绑定到旧 loop，下个测试 setup 拿到这条连接会触发 RuntimeError:
    # Event loop is closed（pytest-asyncio function-scoped loop 的已知 race）。
    # 这里 dispose 清空池，强制下个 connect() 走新建 connection 路径。
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                async with AsyncSessionLocal(bind=conn) as session:
                    yield session
            finally:
                await outer.rollback()
    except RuntimeError as e:
        # asyncpg _terminate_graceful_close 在 loop 关闭后试图创建 cancel task
        # 会抛 RuntimeError（loop closed）。sqlalchemy + asyncpg + pytest-asyncio
        # 已知 teardown race，pre-existing flake，不影响测试结果本身。
        if "Event loop is closed" not in str(e):
            raise
    # teardown：精准清理 ai 测试残留（executor.py 内部独立 session 写的）
    try:
        async with engine.connect() as cleanup_conn:
            await cleanup_conn.execute(
                text("DELETE FROM ai_operation_log WHERE trace_id LIKE 'tr_test%'")
            )
            await cleanup_conn.commit()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise
    # dispose 容错：同上 race 在 engine.dispose() 路径触发的兜底。
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise


@pytest.fixture
async def auth_token(db_session) -> str:
    """构造一个合法 JWT（不通过 /auth/login，直接 jwt.encode，参考 test_refresh_token.py:26）.

    使用 init_db.py 创建的 admin 用户（user_name='admin'，超管）.
    CI 在 pytest 前跑 `python scripts/init_db.py`（.github/workflows/ci.yml:92），
    本地 dev 同样假设已 init（README 标准步骤）.
    """
    from datetime import datetime, timedelta, timezone

    from jose import jwt
    from sqlalchemy import select

    from app.core.config import settings
    from app.modules.system.models.user import User

    user = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {
        "exp": exp,
        "sub": str(user.user_id),
        "user_id": user.user_id,
        "user_name": user.user_name,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _make_agent(code: str, name: str, description: str = "", display_order: int = 0):
    """构造内存 AiAgent-like 对象（不查 DB）."""
    from types import SimpleNamespace

    return SimpleNamespace(
        code=code,
        name=name,
        description=description or f"desc for {code}",
        display_order=display_order,
        agent_id=abs(hash(code)) & 0xFFFFFFFF,  # 占位 ID，仅用于 dedup
        enabled=True,
    )


@pytest.fixture
def mock_visible_agents(monkeypatch):
    """spec §6.3 测试前置：monkeypatch `list_visible_agents` 返回内存对象.

    比 UPDATE ai_agent SET enabled=true 更优：
    - 零 DB 写入，无污染（CLAUDE.md 硬规则 #7 测试隔离）
    - 无 teardown 责任（monkeypatch 自动还原）
    - 无 xdist 并发竞态

    候选 Agent：shared + 6 业务（与 seed_ai_agents.py 一致）.

    Patches 三处引用：
    - api/agent.py（GET /ai/agents）
    - service/agent_visibility.py（chat.py 内 inline import 调用）
    - service/routing_feedback_service.py（module-level import，必须显式 patch 否则不生效）

    第三个 patch 是关键：routing_feedback_service 顶部 `from ... import list_visible_agents`
    在 import 时 bind 到原函数对象；monkeypatch 必须 setattr 该 module 的 attribute
    才能让 service 调用看到 mock。否则 CI 上 seed_ai_agents.py 默认 enabled=False，
    真实 list_visible_agents 返回空集 → correctedAgentCode 不在 visible_codes → 403.
    """
    from app.modules.ai.api import agent as agent_mod
    from app.modules.ai.service import agent_visibility as vis_mod
    from app.modules.ai.service import routing_feedback_service as fb_mod

    candidates = [
        _make_agent("shared", "通用工具助手", "fallback agent", 1),
        _make_agent("user_mgmt", "用户管理助手", "用户 CRUD", 2),
        _make_agent("role_mgmt", "角色权限助手", "角色 CRUD", 3),
        _make_agent("config_mgmt", "系统配置助手", "配置查询", 4),
        _make_agent("dept_mgmt", "部门管理助手", "部门树", 5),
        _make_agent("provider_mgmt", "AI Provider 助手", "Provider 配置", 6),
        _make_agent("job_mgmt", "定时任务助手", "cron job", 7),
    ]

    async def _fake_list(db, user):
        return candidates

    monkeypatch.setattr(agent_mod, "list_visible_agents", _fake_list)
    monkeypatch.setattr(vis_mod, "list_visible_agents", _fake_list)
    monkeypatch.setattr(fb_mod, "list_visible_agents", _fake_list)
    return candidates


@pytest.fixture
async def seed_test_message(auth_token) -> int:
    """创建一条 assistant 消息（用 admin 用户），返回 message_id.

    用例：routing-feedback 测试需要一条已存在的 ai_message 行.
    独立 session 真实 commit；teardown 删除（避免污染其它测试）.
    """
    from sqlalchemy import delete, select

    from app.core.id_generator import next_id
    from app.db.session import AsyncSessionLocal
    from app.modules.ai.models.agent import AiAgent
    from app.modules.ai.models.conversation import AiConversation
    from app.modules.ai.models.message import AiMessage
    from app.modules.system.models.user import User

    async with AsyncSessionLocal() as s:
        user = (
            await s.execute(select(User).where(User.user_name == "admin"))
        ).scalar_one()
        agent = (
            await s.execute(select(AiAgent).where(AiAgent.code == "user_mgmt"))
        ).scalar_one()

        conv_id = next_id()
        msg_id = next_id()
        s.add(
            AiConversation(
                conversation_id=conv_id,
                user_id=user.user_id,
                title="test",
                agent_code=agent.code,
            )
        )
        s.add(
            AiMessage(
                message_id=msg_id,
                conversation_id=conv_id,
                role="assistant",
                message_type="text",
                content="test response",
                agent_code=agent.code,
            )
        )
        await s.commit()

    yield msg_id

    # teardown
    async with AsyncSessionLocal() as s:
        await s.execute(delete(AiMessage).where(AiMessage.message_id == msg_id))
        await s.execute(
            delete(AiConversation).where(AiConversation.conversation_id == conv_id)
        )
        await s.commit()

    # 释放连接池：fixture 用独立 AsyncSessionLocal 真实 commit，
    # pool 里的连接绑到当前 loop；下个测试 setup 前必须 dispose
    # 避免 "Event loop is closed" race（参考 test_executor_integration.py:99-105）
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise


@pytest.fixture
async def seed_test_message_other_user(auth_token) -> int:
    """创建一条属于另一个用户的 assistant 消息，返回 message_id.

    用于 test_admin_can_feedback_other_users_message（spec §6.4: 超管可反馈他人消息）.
    """
    from sqlalchemy import delete, select

    from app.core.id_generator import next_id
    from app.db.session import AsyncSessionLocal
    from app.modules.ai.models.agent import AiAgent
    from app.modules.ai.models.conversation import AiConversation
    from app.modules.ai.models.message import AiMessage
    from app.modules.system.models.user import User

    async with AsyncSessionLocal() as s:
        other = (
            (await s.execute(select(User).where(User.user_name != "admin").limit(1)))
            .scalars()
            .first()
        )
        if other is None:
            # User 模型字段是 hashed_password（不是 password），用 get_password_hash 避免后续 verify 抛 bcrypt 异常
            from app.core.security import get_password_hash  # noqa: PLC0415

            other = User(
                user_id=next_id(),
                user_name=f"other_{next_id()}",
                hashed_password=get_password_hash("x"),
                status="1",
            )
            s.add(other)
            await s.flush()

        agent = (
            await s.execute(select(AiAgent).where(AiAgent.code == "user_mgmt"))
        ).scalar_one()

        conv_id = next_id()
        msg_id = next_id()
        s.add(
            AiConversation(
                conversation_id=conv_id,
                user_id=other.user_id,
                title="other",
                agent_code=agent.code,
            )
        )
        s.add(
            AiMessage(
                message_id=msg_id,
                conversation_id=conv_id,
                role="assistant",
                message_type="text",
                content="other response",
                agent_code=agent.code,
            )
        )
        await s.commit()

    yield msg_id

    async with AsyncSessionLocal() as s:
        await s.execute(delete(AiMessage).where(AiMessage.message_id == msg_id))
        await s.execute(
            delete(AiConversation).where(AiConversation.conversation_id == conv_id)
        )
        await s.commit()

    # 释放连接池：同 seed_test_message
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise
