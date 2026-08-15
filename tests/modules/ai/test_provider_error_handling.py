"""Provider failures stay stable and redacted on the chat stream path."""

import inspect

from app.modules.ai.api import chat as chat_module


def test_chat_stream_maps_provider_failures_before_internal_error() -> None:
    source = inspect.getsource(chat_module)

    provider_branch = source.index("is_provider_failure(exc)")
    internal_branch = source.index('stream_error_code = "AI_INTERNAL_ERROR"')
    assert provider_branch < internal_branch
    assert 'stream_error_code = "AI_PROVIDER_UPSTREAM_ERROR"' in source
    assert '"PydanticAI Provider stream failed"' in source
