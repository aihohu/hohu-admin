"""SafeHttpClient 单测 — 覆盖 spec §SSRF Phase 1 全部要点。"""

from __future__ import annotations

import httpx
import pytest

from app.core.exceptions import SSRFBlockedException
from app.utils.safe_http import (
    MAX_RESPONSE_BYTES,
    SafeHttpClient,
    _validate_ip,
)


def _fake_getaddrinfo(ips: list[str]):
    """构造替代 asyncio.getaddrinfo 的 fake，返回指定 IP 列表。"""

    async def fake(_hostname: str) -> list[tuple]:
        return [(None, None, None, None, (ip, 0)) for ip in ips]

    return fake


def _patch_httpx_with_handler(monkeypatch, handler):
    """让 SafeHttpClient 内部的 httpx.AsyncClient 用 MockTransport。

    SafeHttpClient 在 get() 里 ``httpx.AsyncClient(...)`` 构造客户端，
    我们 monkeypatch 模块里的 httpx.AsyncClient，强制注入 transport。
    注意：必须先捕获原始 AsyncClient 再 patch，否则 factory 调用的是自己 → 递归。
    """
    original_async_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return original_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("app.utils.safe_http.httpx.AsyncClient", factory)


class TestValidateIp:
    """IP 黑名单覆盖 spec §SSRF Phase 1 第 4 条。"""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.169.254",  # 云元数据
            "169.254.0.1",
            "100.64.0.1",
            "0.0.0.0",
        ],
    )
    def test_blocked_ipv4(self, ip):
        with pytest.raises(SSRFBlockedException):
            _validate_ip(ip)

    @pytest.mark.parametrize("ip", ["::1", "fc00::1", "fd00::1", "fe80::1"])
    def test_blocked_ipv6(self, ip):
        with pytest.raises(SSRFBlockedException):
            _validate_ip(ip)

    def test_ipv4_mapped_ipv6_blocked(self):
        # ::ffff:10.0.0.1 必须按 IPv4 规则拒（spec §SSRF Phase 1 第 3 条）
        with pytest.raises(SSRFBlockedException):
            _validate_ip("::ffff:10.0.0.1")

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "114.114.114.114"])
    def test_public_ip_allowed(self, ip):
        _validate_ip(ip)  # 不抛即通过


class TestScheme:
    """协议白名单：spec §SSRF Phase 1 第 1 条。"""

    async def test_file_scheme_rejected(self):
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="协议"):
            await client.get("file:///etc/passwd")

    async def test_gopher_scheme_rejected(self):
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="协议"):
            await client.get("gopher://x")

    async def test_ftp_scheme_rejected(self):
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="协议"):
            await client.get("ftp://x")

    async def test_dict_scheme_rejected(self):
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="协议"):
            await client.get("dict://x")


class TestUrlPattern:
    """URL pattern 白名单：spec §SSRF Phase 1 第 3 条。"""

    async def test_pattern_mismatch_rejected(self):
        # 协议层先过，pattern 层拒；不需要 DNS / httpx
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="pattern"):
            await client.get(
                "https://evil.com/x",
                allowed_pattern=r"^https://api\.weather\.com/",
            )

    async def test_pattern_match_passes_to_dns(self, monkeypatch):
        # pattern 匹配 → 进入 DNS → mock DNS 返回黑名单 IP → 拒
        monkeypatch.setattr(
            "app.utils.safe_http._async_getaddrinfo",
            _fake_getaddrinfo(["127.0.0.1"]),
        )
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="黑名单"):
            await client.get(
                "https://api.weather.com/x",
                allowed_pattern=r"^https://api\.weather\.com/",
            )


class TestBlockedUrls:
    """spec §21.4 test_ssrf_blocked 全量覆盖。"""

    @pytest.mark.parametrize(
        "url,ips",
        [
            ("http://127.0.0.1/", ["127.0.0.1"]),
            ("http://10.0.0.1/", ["10.0.0.1"]),
            ("http://192.168.1.1/", ["192.168.1.1"]),
            ("http://169.254.169.254/", ["169.254.169.254"]),
            ("http://localhost/", ["127.0.0.1"]),
        ],
    )
    async def test_blocked(self, monkeypatch, url, ips):
        monkeypatch.setattr(
            "app.utils.safe_http._async_getaddrinfo", _fake_getaddrinfo(ips)
        )
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="黑名单"):
            await client.get(url)


class TestResponseSize:
    """响应大小限制：spec §SSRF Phase 1 第 6 条。"""

    async def test_oversize_body_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.safe_http._async_getaddrinfo",
            _fake_getaddrinfo(["8.8.8.8"]),
        )
        oversize = b"x" * (MAX_RESPONSE_BYTES + 1)

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=oversize)

        _patch_httpx_with_handler(monkeypatch, handler)
        client = SafeHttpClient()
        with pytest.raises(SSRFBlockedException, match="超过"):
            await client.get("https://api.example.com/")


class TestFollowRedirects:
    """重定向禁用：spec §SSRF Phase 1 第 4 条末段。"""

    async def test_redirect_not_followed(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.safe_http._async_getaddrinfo",
            _fake_getaddrinfo(["8.8.8.8"]),
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                301, headers={"Location": "https://api.example.com/elsewhere"}
            )

        _patch_httpx_with_handler(monkeypatch, handler)
        client = SafeHttpClient()
        resp = await client.get("https://api.example.com/")
        assert resp.status_code == 301  # 不跟随


class TestHappyPath:
    """公网 URL + pattern 匹配 + 正常响应：完整 happy path。"""

    async def test_public_url_returns_response(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.safe_http._async_getaddrinfo",
            _fake_getaddrinfo(["8.8.8.8"]),
        )

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        _patch_httpx_with_handler(monkeypatch, handler)
        client = SafeHttpClient()
        resp = await client.get(
            "https://api.example.com/v1",
            params={"q": "hello"},
            allowed_pattern=r"^https://api\.example\.com/",
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
