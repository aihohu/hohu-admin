"""低代码应用外部 HTTP 调用的 SSRF 防护层。

所有低代码 api_call / x-external-ref 拉外部数据都必须走 SafeHttpClient，
不允许应用代码直接 httpx.get。

当前防护：协议白名单 + URL pattern 白名单 + IP 黑名单 + IPv4-mapped IPv6
双栈校验 + follow_redirects=False + 1MB 响应上限 + 5s/10s 超时。
当前实现只做单次 DNS 解析；如需抵御 DNS rebinding，必须改为解析、校验后直连 IP 并固定 Host。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from ipaddress import (
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from urllib.parse import urlparse

import httpx

from app.core.exceptions import SSRFBlockedException

MAX_RESPONSE_BYTES = 1024 * 1024  # 1MB
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
ALLOWED_SCHEMES = frozenset({"http", "https"})

# 阻止未指定地址、私网、环回、链路本地和运营商共享地址段。
BLOCKED_NETWORKS: tuple[ip_network, ...] = (
    ip_network("0.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),  # 含云元数据 169.254.169.254
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("100.64.0.0/10"),  # CGN
    ip_network("::1/128"),
    ip_network("fc00::/7"),  # ULA
    ip_network("fe80::/10"),  # 链路本地
)


def _validate_ip(
    ip_str: str,
    *,
    allowed_cidrs: Iterable[IPv4Network | IPv6Network] = (),
) -> None:
    """校验单个解析地址；显式 CIDR 可用于受控的本地出站。"""
    addr = ip_address(ip_str)
    # IPv4-mapped IPv6 必须按内嵌 IPv4 再检查，避免绕过 IPv4 黑名单。
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if any(addr.version == net.version and addr in net for net in allowed_cidrs):
        return
    blocked = any(addr in net for net in BLOCKED_NETWORKS)
    blocked = blocked or any(
        (
            addr.is_private,
            addr.is_loopback,
            addr.is_link_local,
            addr.is_multicast,
            addr.is_unspecified,
            addr.is_reserved,
        )
    )
    if blocked:
        raise SSRFBlockedException(f"目标 IP 在黑名单或非公网地址范围：{addr}")


async def _async_getaddrinfo(hostname: str) -> list[tuple]:
    """asyncio.getaddrinfo 薄包装，便于单测 monkeypatch。"""
    return await asyncio.get_running_loop().getaddrinfo(hostname, None)


async def resolve_and_validate_addresses(
    hostname: str,
    *,
    allowed_cidrs: Iterable[IPv4Network | IPv6Network] = (),
) -> tuple[str, ...]:
    """解析并校验全部 DNS 结果；任一不安全答案都整体拒绝。"""
    try:
        literal = ip_address(hostname)
    except ValueError:
        infos = await _async_getaddrinfo(hostname)
        addresses = [str(info[4][0]) for info in infos]
    else:
        addresses = [str(literal)]
    if not addresses:
        raise SSRFBlockedException("目标主机没有可用地址")
    unique_addresses = tuple(dict.fromkeys(addresses))
    for address in unique_addresses:
        _validate_ip(address, allowed_cidrs=allowed_cidrs)
    return unique_addresses


class SafeHttpClient:
    """受限 HTTP 客户端，所有低代码外部请求的唯一出口。"""

    def __init__(self, *, timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        allowed_pattern: str | None = None,
    ) -> httpx.Response:
        """安全 GET。返回 httpx.Response（调用方自己读 .json() / .text()）。

        Args:
            url: 完整 URL，必须 http/https
            params: query 参数（httpx 自动 URL-encode，禁止调用方手工拼）
            allowed_pattern: URL pattern regex，必须匹配 url 才放行。
                调用方应从 manifest permissions[type=external_api].detail.pattern 取。

        Raises:
            SSRFBlockedException: 协议 / pattern / IP 任一校验失败，或响应超 1MB
        """
        # 1. 协议白名单
        parsed = urlparse(url)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise SSRFBlockedException(f"协议不被允许：{parsed.scheme}")
        if not parsed.hostname:
            raise SSRFBlockedException("URL 缺少 hostname")

        # 2. URL pattern 白名单
        if allowed_pattern is not None and not re.match(allowed_pattern, url):
            raise SSRFBlockedException(f"URL 不匹配声明的 pattern：{allowed_pattern}")

        # 3. 单次 DNS 解析 + IP 黑名单；此处尚不抵御解析后换绑。
        await resolve_and_validate_addresses(parsed.hostname)

        # 4. 实际请求：follow_redirects=False + stream 控大小
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
        ) as client:
            async with client.stream("GET", url, params=params) as resp:
                # 边读边累计，超 1MB 立刻抛（避免大响应先吃满内存）
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise SSRFBlockedException(
                            f"响应体超过 {MAX_RESPONSE_BYTES} 字节上限"
                        )
                    chunks.append(chunk)
                # 把读出来的 body 注回 response，调用方 .text() / .json() 可用
                resp._content = b"".join(chunks)
                return resp


# 模块级单例，与项目其他 service 风格一致
safe_http_client = SafeHttpClient()
