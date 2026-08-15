"""AI 模块熔断入口与启动隔离测试。"""

import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_disabled_module_returns_503_without_loading_ai_business_modules() -> None:
    """fresh process 下 false 只留下统一 503 guard，不加载 AI 执行链。"""
    script = r"""
import asyncio
import sys

from httpx import ASGITransport, AsyncClient

from app.main import app


FORBIDDEN_MODULES = {
    "app.modules.ai.api.chat",
    "app.modules.ai.api.provider",
    "app.modules.ai.agents.tools.registry",
    "app.modules.ai.agents.gateway.executor",
    "app.modules.ai.core.provider_registry",
    "app.modules.ai.lifecycle",
    "app.modules.ai.service.agent_visibility",
    "app.modules.ai.service.chat_service",
    "app.modules.ai.service.provider_service",
}


async def verify() -> None:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for method, path in (
                ("GET", "/ai"),
                ("POST", "/ai/chat"),
                ("GET", "/ai/provider/models"),
                ("PATCH", "/ai/anything/deep"),
                ("DELETE", "/ai/conversation/1"),
                ("OPTIONS", "/ai/chat"),
                ("TRACE", "/ai/chat"),
                ("CONNECT", "/ai/chat"),
            ):
                response = await client.request(method, path)
                assert response.status_code == 503, (method, path, response.text)
                body = response.json()
                assert body["code"] == 503
                assert body["data"] is None
                assert body["errorCode"] == "AI_MODULE_DISABLED"
                assert isinstance(body["msg"], str) and body["msg"]

    imported = sorted(FORBIDDEN_MODULES.intersection(sys.modules))
    assert imported == [], imported


asyncio.run(verify())
"""
    env = os.environ.copy()
    env.update(
        {
            "AI_MODULE_ENABLED": "false",
            "APP_ROLE": "api",
            "ENV": "test",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"disabled-process verification failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
