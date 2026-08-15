"""AI 提供商模型注册表

根据提供商配置动态创建 Pydantic AI Model 实例。
"""

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.core.config import settings
from app.modules.ai.core.provider_egress import (
    get_provider_http_client,
    provider_egress,
)


def create_model(
    provider_code: str, model_name: str, api_key: str, base_url: str | None = None
):
    """根据提供商配置创建 Pydantic AI Model 实例

    Args:
        provider_code: 提供商标识（openai / anthropic / deepseek）
        model_name: 模型名称（如 gpt-4o、claude-sonnet-4-6）
        api_key: API Key
        base_url: API 地址（OpenAI 兼容协议留空用默认）

    Returns:
        Pydantic AI Model 实例
    """
    effective_base_url = provider_egress.effective_base_url(provider_code, base_url)
    provider_egress.validate_url_static(effective_base_url)
    http_client = get_provider_http_client()

    if provider_code in ("openai", "deepseek"):
        openai_client = AsyncOpenAI(
            base_url=effective_base_url,
            api_key=api_key,
            http_client=http_client,
            max_retries=settings.AI_PROVIDER_EGRESS_MAX_RETRIES,
        )
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(openai_client=openai_client),
        )
    elif provider_code == "anthropic":
        from anthropic import AsyncAnthropic  # noqa: PLC0415
        from pydantic_ai.models.anthropic import AnthropicModel  # noqa: PLC0415
        from pydantic_ai.providers.anthropic import AnthropicProvider  # noqa: PLC0415

        anthropic_client = AsyncAnthropic(
            api_key=api_key,
            base_url=effective_base_url,
            http_client=http_client,
            max_retries=settings.AI_PROVIDER_EGRESS_MAX_RETRIES,
        )
        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(anthropic_client=anthropic_client),
        )
    else:
        # 默认走 OpenAI 兼容协议
        openai_client = AsyncOpenAI(
            base_url=effective_base_url,
            api_key=api_key,
            http_client=http_client,
            max_retries=settings.AI_PROVIDER_EGRESS_MAX_RETRIES,
        )
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(openai_client=openai_client),
        )


def get_default_model():
    """获取默认模型（从 .env 配置）

    Returns:
        Pydantic AI Model 实例，如果未配置 API Key 则返回 None
    """
    model_str = settings.AI_DEFAULT_MODEL
    parts = model_str.split(":", 1)
    provider_code = parts[0] if len(parts) > 1 else "openai"
    model_name = parts[1] if len(parts) > 1 else model_str

    if provider_code == "openai" and settings.AI_OPENAI_API_KEY:
        return create_model(
            provider_code,
            model_name,
            api_key=settings.AI_OPENAI_API_KEY,
            base_url=settings.AI_OPENAI_BASE_URL or None,
        )
    elif provider_code == "anthropic" and settings.AI_ANTHROPIC_API_KEY:
        return create_model(
            provider_code,
            model_name,
            api_key=settings.AI_ANTHROPIC_API_KEY,
        )

    return None
