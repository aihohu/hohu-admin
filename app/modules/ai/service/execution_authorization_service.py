"""Final live authorization check inside an uncommitted AI write transaction."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ensure_ai_chat_use
from app.core.config import settings
from app.core.exceptions import AuthorizationException
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import ChatDeps
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority


async def ensure_current_write_authority(
    db: AsyncSession, *, deps: ChatDeps, meta: AiToolMeta
) -> None:
    if not settings.AI_MODULE_ENABLED:
        raise AuthorizationException("AI 助手已关闭", error_code="AI_MODULE_DISABLED")
    current = await load_live_user_authority(db, tenant=deps.tenant)
    if current.auth_version != deps.user.auth_version:
        raise AuthorizationException(
            "当前登录授权已失效", error_code="AI_TOOL_PERM_DENIED"
        )
    ensure_ai_chat_use(current)
    if not set(meta.required_perms) <= agent_authorization_service.tool_permissions(
        current
    ):
        raise AuthorizationException(
            "权限已被撤回，本次修改未提交", error_code="AI_TOOL_PERM_DENIED"
        )
    await agent_authorization_service.authorize_agent_access(db, current, meta.agent)
    if deps.data_scope_hash is not None:
        current_scope = await result_projection_service.compute_data_scope_hash(
            db, current
        )
        if current_scope != deps.data_scope_hash:
            raise AuthorizationException(
                "数据权限已变化，本次修改未提交", error_code="AI_DATA_SCOPE_VIOLATION"
            )
