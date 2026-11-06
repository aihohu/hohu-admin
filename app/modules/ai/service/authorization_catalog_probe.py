"""One bounded management probe with private SDK/client retry configuration."""

import asyncio

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.core.exceptions import BusinessException
from app.modules.ai.core.generation_settings import generation_settings
from app.modules.ai.core.provider_egress import (
    build_provider_http_client,
    provider_egress,
    provider_upstream_error,
)


async def probe(effective):
    """No Session is accepted; callers must release their preparation transaction."""
    provider_code = effective["provider_code"]
    model = effective["model"]
    provider_egress.validate_adapter_config(effective["config"])
    provider_egress.validate_adapter_config(model.get("config"))
    generation_settings(model.get("config"))
    try:
        async with asyncio.timeout(15):
            async with asyncio.timeout(2):
                await provider_egress.validate_destination(
                    provider_code, effective["base_url"]
                )
                if model.get("base_url"):
                    await provider_egress.validate_destination(
                        provider_code, model["base_url"]
                    )
            base_url = provider_egress.effective_base_url(
                provider_code, model.get("base_url") or effective["base_url"]
            )
            async with build_provider_http_client() as client:
                if provider_code == "anthropic":
                    from anthropic import AsyncAnthropic  # noqa: PLC0415
                    from pydantic_ai.models.anthropic import (  # noqa: PLC0415
                        AnthropicModel,
                    )
                    from pydantic_ai.providers.anthropic import (  # noqa: PLC0415
                        AnthropicProvider,
                    )

                    sdk = AsyncAnthropic(
                        api_key=effective["api_key"],
                        base_url=base_url,
                        http_client=client,
                        max_retries=0,
                    )
                    instance = AnthropicModel(
                        model["name"], provider=AnthropicProvider(anthropic_client=sdk)
                    )
                else:
                    sdk = AsyncOpenAI(
                        api_key=effective["api_key"],
                        base_url=base_url,
                        http_client=client,
                        max_retries=0,
                    )
                    instance = OpenAIChatModel(
                        model["name"], provider=OpenAIProvider(openai_client=sdk)
                    )
                await Agent(instance, instructions="Reply with OK", retries=0).run(
                    "Say OK",
                    model_settings={"max_tokens": 16},
                    usage_limits=UsageLimits(request_limit=1, output_tokens_limit=16),
                )
    except BusinessException:
        raise
    except Exception:
        raise provider_upstream_error() from None
