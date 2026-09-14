"""Marketplace 和 Lowcode 路由排除测试。

Marketplace、Contributes 与 Lowcode app-data 源码保留，但当前应用不注册相关
HTTP 入口。请求必须得到 404，而不是进入鉴权或业务逻辑。
"""

import pytest


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/marketplace/list", {}),
        ("get", "/marketplace/search", {"params": {"keyword": "x"}}),
        ("get", "/marketplace/detail/any-slug", {}),
        ("post", "/marketplace/install", {"json": {"appSlug": "any-app"}}),
        ("post", "/marketplace/uninstall/any-slug", {}),
        ("post", "/marketplace/enable/any-slug", {}),
        ("post", "/marketplace/disable/any-slug", {}),
        ("get", "/marketplace/installed", {}),
        ("post", "/marketplace/rating", {"json": {"appId": "1", "rating": 5}}),
        ("get", "/api/v1/contributes/", {}),
        ("post", "/api/v1/app-data/demo/_", {"json": {"name": "blocked"}}),
    ],
)
async def test_deferred_marketplace_and_lowcode_routes_are_not_mounted(
    client, method, path, kwargs
):
    response = await getattr(client, method)(path, **kwargs)

    assert response.status_code == 404


async def test_bearer_token_cannot_restore_deferred_routes(client):
    response = await client.get(
        "/marketplace/list",
        headers={"Authorization": "Bearer fake.jwt.token"},
    )

    assert response.status_code == 404
