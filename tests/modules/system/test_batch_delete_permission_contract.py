"""The tool advertises the same explicit grant its fixed writer requires."""

from app.modules.ai.constants import PUBLISHED_AGENT_TOOL_PERMISSIONS
from app.modules.system.ai_tools.user_management import user_batch_delete
from app.modules.system.service.authorization_system_candidate import (
    SYSTEM_CANDIDATE_ENTRIES,
)


def test_batch_delete_tool_and_writer_require_the_same_permission():
    required = SYSTEM_CANDIDATE_ENTRIES["user.batch_delete"].permissions
    assert required == ("system:user:batch-delete",)
    assert user_batch_delete.__ai_tool_meta__.required_perms == required
    assert set(required) <= PUBLISHED_AGENT_TOOL_PERMISSIONS
