"""Provider 出站目的地 Policy 与所有 SDK 共用的 hardened transport。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import BusinessException, BusinessRuleException
from app.utils.safe_http import resolve_and_validate_addresses

logger = logging.getLogger(__name__)

CANONICAL_PROVIDER_BASE_URLS: Mapping[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com/v1",
}
CANONICAL_PROVIDER_ORIGINS = frozenset(
    {
        "https://api.openai.com:443",
        "https://api.anthropic.com:443",
        "https://api.deepseek.com:443",
    }
)
_FORBIDDEN_CONFIG_KEY_PARTS = (
    "proxy",
    "transport",
    "endpoint",
    "baseurl",
    "httpclient",
    "followredirect",
    "timeout",
    "retries",
    "verify",
    "certificate",
    "authorization",
    "bearertoken",
    "credential",
    "password",
    "secret",
    "apikey",
    "cookie",
    "headers",
)
_SANITIZED_UPSTREAM_BODY = json.dumps(
    {"error": {"message": "provider request failed"}}, separators=(",", ":")
).encode()


def _forbidden() -> BusinessRuleException:
    return BusinessRuleException(
        "Provider 出站地址不符合安全策略",
        error_code="AI_PROVIDER_URL_FORBIDDEN",
    )


def provider_upstream_error() -> BusinessException:
    error = BusinessRuleException(
        "Provider 暂时不可用，请稍后重试",
        error_code="AI_PROVIDER_UPSTREAM_ERROR",
    )
    error.code = 502
    return error


def is_provider_failure(exc: BaseException) -> bool:
    """识别 SDK 包装后的 Provider 失败，不读取或返回异常文本。"""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ProviderTransportError):
            return True
        module = type(current).__module__
        name = type(current).__name__
        if module.startswith(("openai", "anthropic")) and (
            name.endswith("Error") or name.endswith("Exception")
        ):
            return True
        if module.startswith("pydantic_ai") and name in {
            "ModelHTTPError",
            "UnexpectedModelBehavior",
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class ValidatedProviderUrl:
    url: str
    origin: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


class ProviderTransportError(httpx.TransportError):
    """不携带 URL、上游 body 或底层异常文本的稳定 transport 错误。"""

    def __init__(
        self,
        category: str,
        *,
        request: httpx.Request | None = None,
    ) -> None:
        self.category = category
        super().__init__("provider request failed", request=request)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _origin_for(url: httpx.URL) -> str:
    host = url.host.lower()
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if url.scheme == "https" else 80
    return f"{url.scheme}://{display_host}:{url.port or default_port}"


def _parse_origin(value: str) -> str:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError) as exc:
        raise _forbidden() from exc
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {b"", b"/", "", "/"}
    ):
        raise _forbidden()
    return _origin_for(url)


class ProviderEgressPolicy:
    """Deployment-owned allowlist plus DNS/IP validation."""

    def __init__(
        self,
        *,
        allowed_origins: Iterable[str] | None = None,
        allowed_cidrs: Iterable[str] | None = None,
        resolver: Callable[[str], Awaitable[list[tuple]]] | None = None,
    ) -> None:
        configured_origins = (
            tuple(allowed_origins)
            if allowed_origins is not None
            else _split_csv(settings.AI_PROVIDER_EGRESS_ALLOWED_ORIGINS)
        )
        self.allowed_origins = CANONICAL_PROVIDER_ORIGINS | {
            _parse_origin(origin) for origin in configured_origins
        }
        cidr_values = (
            tuple(allowed_cidrs)
            if allowed_cidrs is not None
            else _split_csv(settings.AI_PROVIDER_EGRESS_ALLOWED_CIDRS)
        )
        try:
            self.allowed_cidrs: tuple[IPv4Network | IPv6Network, ...] = tuple(
                ip_network(value, strict=True) for value in cidr_values
            )
        except ValueError as exc:
            raise RuntimeError("Invalid AI provider egress CIDR configuration") from exc
        self._resolver = resolver

    def effective_base_url(
        self,
        provider_code: str,
        base_url: str | None,
    ) -> str:
        explicit = (base_url or "").strip()
        if explicit:
            return explicit
        canonical = CANONICAL_PROVIDER_BASE_URLS.get(provider_code.lower())
        if canonical is None:
            raise _forbidden()
        return canonical

    def validate_url_static(self, url_value: str) -> tuple[httpx.URL, str]:
        try:
            url = httpx.URL(url_value)
        except (httpx.InvalidURL, ValueError) as exc:
            raise _forbidden() from exc
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise _forbidden()
        origin = _origin_for(url)
        if origin not in self.allowed_origins:
            raise _forbidden()
        return url, origin

    async def _resolve(self, hostname: str) -> tuple[str, ...]:
        if self._resolver is None:
            return await resolve_and_validate_addresses(
                hostname,
                allowed_cidrs=self.allowed_cidrs,
            )
        try:
            literal = ip_address(hostname)
        except ValueError:
            infos = await self._resolver(hostname)
            addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
        else:
            addresses = (str(literal),)
        if not addresses:
            raise _forbidden()
        # Reuse the shared classification by resolving literals one at a time.
        for address in addresses:
            try:
                await resolve_and_validate_addresses(
                    address,
                    allowed_cidrs=self.allowed_cidrs,
                )
            except Exception as exc:
                raise _forbidden() from exc
        return addresses

    async def validate_url(self, url_value: str) -> ValidatedProviderUrl:
        url, origin = self.validate_url_static(url_value)
        try:
            async with asyncio.timeout(settings.AI_PROVIDER_EGRESS_CONNECT_TIMEOUT_SEC):
                addresses = await self._resolve(url.host)
        except BusinessException:
            raise
        except TimeoutError as exc:
            raise _forbidden() from exc
        except Exception as exc:
            raise _forbidden() from exc
        if url.scheme == "http":
            # Plain HTTP is only for explicitly controlled local models: every
            # resolved address must be covered by a deployment-owned CIDR.
            for address in addresses:
                parsed = ip_address(address)
                if not any(
                    parsed.version == network.version and parsed in network
                    for network in self.allowed_cidrs
                ):
                    raise _forbidden()
        return ValidatedProviderUrl(
            url=str(url),
            origin=origin,
            hostname=url.host,
            port=url.port or (443 if url.scheme == "https" else 80),
            addresses=addresses,
        )

    async def validate_destination(
        self,
        provider_code: str,
        base_url: str | None,
    ) -> ValidatedProviderUrl:
        return await self.validate_url(self.effective_base_url(provider_code, base_url))

    async def is_destination_allowed(
        self,
        provider_code: str,
        base_url: str | None,
    ) -> bool:
        try:
            await self.validate_destination(provider_code, base_url)
        except BusinessException:
            return False
        return True

    async def is_configuration_allowed(
        self,
        provider_code: str,
        provider_base_url: str | None,
        *,
        model_base_url: str | None = None,
        configs: Iterable[Any] = (),
    ) -> bool:
        try:
            for config in configs:
                self.validate_adapter_config(config)
        except BusinessException:
            return False
        if not await self.is_destination_allowed(provider_code, provider_base_url):
            return False
        if model_base_url and not await self.is_destination_allowed(
            provider_code, model_base_url
        ):
            return False
        return True

    async def is_model_allowed(
        self,
        provider_code: str,
        provider_base_url: str | None,
        *,
        model_base_url: str | None = None,
        provider_config: Any = None,
        model_config: Any = None,
        provider_id: int,
        model_id: int,
    ) -> bool:
        allowed = await self.is_configuration_allowed(
            provider_code,
            provider_base_url,
            model_base_url=model_base_url,
            configs=(provider_config, model_config),
        )
        if allowed:
            return True
        logger.warning(
            "AI Provider egress blocked provider_id=%s model_id=%s category=policy",
            provider_id,
            model_id,
        )
        try:
            from app.modules.ai.metrics import record_security_event  # noqa: PLC0415

            record_security_event("provider_egress_blocked")
        except Exception:  # pragma: no cover - metrics must never weaken fail-closed
            logger.debug("provider egress metric unavailable", exc_info=True)
        return False

    def validate_adapter_config(self, config: Any) -> None:
        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                for raw_key, nested in value.items():
                    normalized = "".join(
                        char for char in str(raw_key).lower() if char.isalnum()
                    )
                    if any(part in normalized for part in _FORBIDDEN_CONFIG_KEY_PARTS):
                        raise _forbidden()
                    walk(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    walk(nested)

        if config is not None:
            walk(config)


class _LimitedResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        source: httpx.AsyncByteStream,
        *,
        max_bytes: int,
        deadline: float,
        release: Callable[[], None],
        request: httpx.Request,
    ) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self._deadline = deadline
        self._release = release
        self._request = request
        self._released = False

    def _release_once(self) -> None:
        if not self._released:
            self._released = True
            self._release()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        iterator = self._source.__aiter__()
        try:
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderTransportError("total_timeout", request=self._request)
                try:
                    async with asyncio.timeout(remaining):
                        chunk = await anext(iterator)
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    raise ProviderTransportError(
                        "total_timeout", request=self._request
                    ) from exc
                total += len(chunk)
                if total > self._max_bytes:
                    raise ProviderTransportError(
                        "response_too_large", request=self._request
                    )
                yield chunk
        finally:
            self._release_once()
            await self._source.aclose()

    async def aclose(self) -> None:
        self._release_once()
        await self._source.aclose()


class ProviderEgressTransport(httpx.AsyncBaseTransport):
    """Resolve, validate, pin, bound, and sanitize every Provider request."""

    def __init__(
        self,
        *,
        policy: ProviderEgressPolicy | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
        inner_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        max_response_bytes: int | None = None,
        total_timeout: float | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        if inner is not None and inner_factory is not None:
            raise ValueError("inner and inner_factory are mutually exclusive")
        self.policy = policy or provider_egress
        self._inner_factory = inner_factory or (
            (lambda: inner) if inner is not None else self._build_inner
        )
        # The pinned URL is keyed by IP inside httpcore. Separate pools by the
        # original validated origin so one hostname can never reuse a TLS
        # connection authenticated with another hostname's SNI.
        self._inners: dict[str, httpx.AsyncBaseTransport] = {}
        self._inner_lock = asyncio.Lock()
        self._max_response_bytes = (
            max_response_bytes
            if max_response_bytes is not None
            else settings.AI_PROVIDER_EGRESS_MAX_RESPONSE_BYTES
        )
        self._total_timeout = (
            total_timeout
            if total_timeout is not None
            else settings.AI_PROVIDER_EGRESS_TOTAL_TIMEOUT_SEC
        )
        self._semaphore = asyncio.Semaphore(
            max_concurrency
            if max_concurrency is not None
            else settings.AI_PROVIDER_EGRESS_MAX_CONCURRENCY
        )

    @staticmethod
    def _build_inner() -> httpx.AsyncBaseTransport:
        return httpx.AsyncHTTPTransport(
            trust_env=False,
            retries=0,
            proxy=settings.AI_PROVIDER_EGRESS_PROXY or None,
        )

    async def _inner_for_origin(self, origin: str) -> httpx.AsyncBaseTransport:
        inner = self._inners.get(origin)
        if inner is not None:
            return inner
        async with self._inner_lock:
            inner = self._inners.get(origin)
            if inner is None:
                inner = self._inner_factory()
                self._inners[origin] = inner
            return inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        deadline = time.monotonic() + self._total_timeout
        try:
            async with asyncio.timeout(self._total_timeout):
                validated = await self.policy.validate_url(str(request.url))
        except BusinessException as exc:
            raise ProviderTransportError("policy_blocked", request=request) from exc
        except TimeoutError as exc:
            raise ProviderTransportError("total_timeout", request=request) from exc
        inner = await self._inner_for_origin(validated.origin)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await self._semaphore.acquire()
        except TimeoutError as exc:
            raise ProviderTransportError("total_timeout", request=request) from exc

        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._semaphore.release()

        address = validated.addresses[0]
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = validated.hostname
        headers = request.headers.copy()
        # The stream limit below observes wire bytes. Disable content encoding
        # and reject non-compliant compressed responses before HTTPX/SDK decoding
        # so decompression cannot expand past the configured response budget.
        headers["accept-encoding"] = "identity"
        pinned_request = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                response = await inner.handle_async_request(pinned_request)
        except TimeoutError as exc:
            release()
            raise ProviderTransportError("total_timeout", request=request) from exc
        except httpx.HTTPError as exc:
            release()
            raise ProviderTransportError("network_error", request=request) from exc
        except BaseException:
            release()
            raise

        if 300 <= response.status_code < 400:
            try:
                await response.aclose()
            finally:
                release()
            raise ProviderTransportError("redirect_blocked", request=request)
        if response.status_code >= 400:
            try:
                await response.aclose()
            finally:
                release()
            return httpx.Response(
                response.status_code,
                content=_SANITIZED_UPSTREAM_BODY,
                headers={"content-type": "application/json"},
                extensions=response.extensions,
            )

        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        if content_encoding and content_encoding != "identity":
            try:
                await response.aclose()
            finally:
                release()
            raise ProviderTransportError("encoded_response_blocked", request=request)

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                oversized = int(content_length) > self._max_response_bytes
            except ValueError:
                oversized = False
            if oversized:
                try:
                    await response.aclose()
                finally:
                    release()
                raise ProviderTransportError("response_too_large", request=request)

        response.stream = _LimitedResponseStream(
            response.stream,
            max_bytes=self._max_response_bytes,
            deadline=deadline,
            release=release,
            request=request,
        )
        return response

    async def aclose(self) -> None:
        async with self._inner_lock:
            unique_inners = {id(inner): inner for inner in self._inners.values()}
            self._inners.clear()
        if unique_inners:
            await asyncio.gather(*(inner.aclose() for inner in unique_inners.values()))


provider_egress = ProviderEgressPolicy()
_http_clients: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, httpx.AsyncClient
] = weakref.WeakKeyDictionary()


def build_provider_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ProviderEgressTransport(),
        timeout=httpx.Timeout(
            connect=settings.AI_PROVIDER_EGRESS_CONNECT_TIMEOUT_SEC,
            read=settings.AI_PROVIDER_EGRESS_READ_TIMEOUT_SEC,
            write=settings.AI_PROVIDER_EGRESS_READ_TIMEOUT_SEC,
            pool=settings.AI_PROVIDER_EGRESS_CONNECT_TIMEOUT_SEC,
        ),
        follow_redirects=False,
        trust_env=False,
    )


def get_provider_http_client() -> httpx.AsyncClient:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return build_provider_http_client()
    client = _http_clients.get(loop)
    if client is None or client.is_closed:
        client = build_provider_http_client()
        _http_clients[loop] = client
    return client


async def close_provider_http_clients() -> None:
    clients = list(_http_clients.values())
    _http_clients.clear()
    if clients:
        await asyncio.gather(*(client.aclose() for client in clients))
