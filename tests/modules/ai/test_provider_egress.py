"""P1-C Provider hardened egress policy and transport."""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.ai.core.provider_egress import (
    ProviderEgressPolicy,
    ProviderEgressTransport,
    ProviderTransportError,
    close_provider_http_clients,
    is_provider_failure,
)
from app.modules.ai.core.provider_registry import create_model


def _resolver(*addresses: str):
    async def resolve(_hostname: str) -> list[tuple]:
        return [(None, None, None, None, (address, 0)) for address in addresses]

    return resolve


def _policy(
    *origins: str,
    addresses: tuple[str, ...] = ("93.184.216.34",),
    cidrs: tuple[str, ...] = (),
) -> ProviderEgressPolicy:
    return ProviderEgressPolicy(
        allowed_origins=origins,
        allowed_cidrs=cidrs,
        resolver=_resolver(*addresses),
    )


async def test_policy_accepts_exact_https_origin_and_normalizes_url() -> None:
    policy = _policy("https://models.example.com:443")

    validated = await policy.validate_url("https://models.example.com/v1/")

    assert validated.origin == "https://models.example.com:443"
    assert validated.addresses == ("93.184.216.34",)
    assert validated.url == "https://models.example.com/v1/"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@models.example.com/v1",
        "https://unlisted.example.com/v1",
        "file:///etc/passwd",
        "https://models.example.com/v1?endpoint=https://evil.example",
    ],
)
async def test_policy_rejects_userinfo_unlisted_and_noncanonical_urls(url: str) -> None:
    policy = _policy("https://models.example.com:443")

    with pytest.raises(BusinessRuleException) as exc_info:
        await policy.validate_url(url)

    assert exc_info.value.error_code == "AI_PROVIDER_URL_FORBIDDEN"
    assert url not in exc_info.value.message


async def test_policy_rejects_when_any_dns_answer_is_private() -> None:
    policy = _policy(
        "https://models.example.com:443",
        addresses=("93.184.216.34", "127.0.0.1"),
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await policy.validate_url("https://models.example.com/v1")

    assert exc_info.value.error_code == "AI_PROVIDER_URL_FORBIDDEN"


async def test_local_http_requires_exact_origin_and_cidr() -> None:
    allowed = _policy(
        "http://models.internal:8080",
        addresses=("10.20.30.40",),
        cidrs=("10.20.30.0/24",),
    )
    blocked = _policy(
        "http://models.internal:8080",
        addresses=("10.20.30.40",),
    )

    validated = await allowed.validate_url("http://models.internal:8080/v1")
    assert validated.addresses == ("10.20.30.40",)
    with pytest.raises(BusinessRuleException):
        await blocked.validate_url("http://models.internal:8080/v1")


def test_transport_config_keys_cannot_expand_network_policy() -> None:
    policy = _policy("https://models.example.com:443")

    for config in (
        {"proxy": "http://127.0.0.1:8080"},
        {"transport": {"verify": False}},
        {"endpoint_url": "https://evil.example"},
        {"nested": {"baseUrl": "https://evil.example"}},
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            policy.validate_adapter_config(config)
        assert exc_info.value.error_code == "AI_PROVIDER_URL_FORBIDDEN"


class _CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.request: httpx.Request | None = None
        self.response = response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return self.response or httpx.Response(200, content=b"ok")


async def test_transport_pins_validated_ip_and_preserves_host_and_tls_sni() -> None:
    inner = _CaptureTransport()
    transport = ProviderEgressTransport(
        policy=_policy("https://models.example.com:443"),
        inner=inner,
        max_response_bytes=1024,
        total_timeout=5,
        max_concurrency=2,
    )
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.get("https://models.example.com/v1/models")

    assert response.text == "ok"
    assert inner.request is not None
    assert inner.request.url.host == "93.184.216.34"
    assert inner.request.headers["host"] == "models.example.com"
    assert inner.request.extensions["sni_hostname"] == "models.example.com"


async def test_transport_rejects_redirect_without_following_location() -> None:
    inner = _CaptureTransport(
        httpx.Response(
            302,
            headers={"location": "https://evil.example/metadata"},
            content=b"redirect",
        )
    )
    transport = ProviderEgressTransport(
        policy=_policy("https://models.example.com:443"),
        inner=inner,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderTransportError) as exc_info:
            await client.get("https://models.example.com/v1")

    assert exc_info.value.category == "redirect_blocked"
    assert "evil.example" not in str(exc_info.value)


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


async def test_transport_enforces_streaming_response_size_limit() -> None:
    inner = _CaptureTransport(
        httpx.Response(200, stream=_ChunkStream([b"123", b"456"]))
    )
    transport = ProviderEgressTransport(
        policy=_policy("https://models.example.com:443"),
        inner=inner,
        max_response_bytes=5,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderTransportError) as exc_info:
            await client.get("https://models.example.com/v1")

    assert exc_info.value.category == "response_too_large"


async def test_transport_sanitizes_upstream_error_body() -> None:
    inner = _CaptureTransport(
        httpx.Response(
            500,
            content=b'{"error":"secret upstream body sk-sensitive-value"}',
        )
    )
    transport = ProviderEgressTransport(
        policy=_policy("https://models.example.com:443"),
        inner=inner,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://models.example.com/v1")

    assert response.status_code == 500
    assert response.json() == {"error": {"message": "provider request failed"}}
    assert "sensitive" not in response.text


class _SlowTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=b"late")


async def test_transport_enforces_total_timeout() -> None:
    transport = ProviderEgressTransport(
        policy=_policy("https://models.example.com:443"),
        inner=_SlowTransport(),
        total_timeout=0.01,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderTransportError) as exc_info:
            await client.get("https://models.example.com/v1")

    assert exc_info.value.category == "total_timeout"


async def test_transport_total_timeout_also_bounds_dns_resolution() -> None:
    async def slow_resolver(_hostname: str) -> list[tuple]:
        await asyncio.sleep(0.05)
        return [(None, None, None, None, ("93.184.216.34", 0))]

    policy = ProviderEgressPolicy(
        allowed_origins=("https://models.example.com:443",),
        resolver=slow_resolver,
    )
    transport = ProviderEgressTransport(policy=policy, total_timeout=0.01)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderTransportError) as exc_info:
            await client.get("https://models.example.com/v1")

    assert exc_info.value.category == "total_timeout"


async def test_transport_isolates_connection_pools_by_original_origin() -> None:
    created: list[_CaptureTransport] = []

    def inner_factory() -> httpx.AsyncBaseTransport:
        inner = _CaptureTransport()
        created.append(inner)
        return inner

    transport = ProviderEgressTransport(
        policy=_policy(
            "https://models-a.example.com:443",
            "https://models-b.example.com:443",
        ),
        inner_factory=inner_factory,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://models-a.example.com/v1")
        await client.get("https://models-b.example.com/v1")

    assert len(created) == 2
    assert created[0].request is not None
    assert created[1].request is not None
    assert created[0].request.url.host == created[1].request.url.host
    assert created[0].request.extensions["sni_hostname"] == "models-a.example.com"
    assert created[1].request.extensions["sni_hostname"] == "models-b.example.com"


class _CancelThenRespondTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, content=b"ok")


async def test_transport_releases_concurrency_permit_when_request_is_cancelled() -> (
    None
):
    inner = _CancelThenRespondTransport()
    transport = ProviderEgressTransport(
        policy=_policy("https://models.example.com:443"),
        inner=inner,
        total_timeout=0.05,
        max_concurrency=1,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        first = asyncio.create_task(client.get("https://models.example.com/v1"))
        await inner.started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        response = await client.get("https://models.example.com/v1")

    assert response.text == "ok"
    assert inner.calls == 2


async def test_transport_rejects_compressed_response_before_sdk_decoding() -> None:
    compressed = gzip.compress(b"x" * 100_000)
    assert len(compressed) < 1_000
    inner = _CaptureTransport(
        httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=compressed,
        )
    )
    transport = ProviderEgressTransport(
        policy=_policy("https://models.example.com:443"),
        inner=inner,
        max_response_bytes=1_000,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderTransportError) as exc_info:
            await client.get("https://models.example.com/v1")

    assert exc_info.value.category == "encoded_response_blocked"
    assert inner.request is not None
    assert inner.request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("provider_code", "base_url"),
    [
        ("openai", "https://api.openai.com/v1"),
        ("anthropic", "https://api.anthropic.com"),
        ("deepseek", "https://api.deepseek.com/v1"),
    ],
)
async def test_every_adapter_injects_same_hardened_client(
    provider_code: str,
    base_url: str,
) -> None:
    model = create_model(provider_code, "test-model", "test-key", base_url)
    sdk_client = model._provider.client
    http_client = sdk_client._client

    assert isinstance(http_client._transport, ProviderEgressTransport)
    assert http_client._trust_env is False
    assert http_client.follow_redirects is False
    assert sdk_client.max_retries == 1
    await close_provider_http_clients()


def test_provider_failure_classifier_walks_wrapped_exception_chain() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    transport_error = ProviderTransportError("network_error", request=request)
    try:
        raise RuntimeError("sdk wrapper") from transport_error
    except RuntimeError as wrapped:
        assert is_provider_failure(wrapped) is True
