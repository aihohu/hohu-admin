from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import AuthorizationException
from app.core.tenant import PlatformContext
from app.modules.ai.schemas.agent_admin import AgentAdminUpdateReq
from app.modules.ai.service.agent_admin import agent_admin_service
from app.modules.platform.constants import PLATFORM_AI_READ


async def test_read_only_platform_principal_cannot_mutate_ai_configuration():
    read_only = PlatformContext(
        actor_principal_id=71,
        actor_name="platform-auditor",
        principal_type="human",
        permissions=frozenset({PLATFORM_AI_READ}),
        reason="Review AI Agent settings",
        ticket_id="SEC-READ-ONLY",
        correlation_id="read-only-write-attempt",
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await agent_admin_service.update_agent(
            AsyncMock(),
            1,
            AgentAdminUpdateReq(name="Forbidden update"),
            platform=read_only,
        )

    assert exc_info.value.error_code == "PLATFORM_PERMISSION_DENIED"
