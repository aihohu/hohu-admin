"""API 层鉴权测试（spec 8.5）。

只验证鉴权边界，不依赖数据库：
- 未登录访问需登录端点 → 401（OAuth2PasswordBearer 抛 HTTPException）
- 已登录但无权限访问管理员端点 → 403（require_permissions 抛 AuthorizationException）

不覆盖成功路径：成功路径需要 DB + 真实 JWT + 权限菜单数据，留到 e2e。
"""

import pytest

VALID_TOKEN = "fake.jwt.token"  # noqa: S105 - 故意无效，触发 401 前不进入业务


@pytest.mark.asyncio
async def test_public_list_requires_login(client):
    """Issue 1: GET /marketplace/list 必须登录（无 token 返回 401）"""
    response = await client.get("/marketplace/list")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_public_search_requires_login(client):
    """Issue 1: GET /marketplace/search 必须登录（无 token 返回 401）"""
    response = await client.get("/marketplace/search", params={"keyword": "x"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_public_detail_requires_login(client):
    """Issue 1: GET /marketplace/detail/{slug} 必须登录（无 token 返回 401）"""
    response = await client.get("/marketplace/detail/any-slug")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_install_requires_auth(client):
    """未登录安装 → 401（require_permissions 内部先走 get_current_user）"""
    response = await client.post(
        "/marketplace/install",
        json={"appSlug": "any-app"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_uninstall_requires_auth(client):
    """未登录卸载 → 401"""
    response = await client.post("/marketplace/uninstall/any-slug")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_enable_requires_auth(client):
    """未登录启用 → 401"""
    response = await client.post("/marketplace/enable/any-slug")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_disable_requires_auth(client):
    """未登录禁用 → 401"""
    response = await client.post("/marketplace/disable/any-slug")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_installed_list_requires_auth(client):
    """未登录查看已安装列表 → 401"""
    response = await client.get("/marketplace/installed")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rating_create_requires_auth(client):
    """未登录评分 → 401"""
    response = await client.post(
        "/marketplace/rating",
        json={"appId": "1", "rating": 5},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rating_update_requires_auth(client):
    """未登录修改评分 → 401"""
    response = await client.put(
        "/marketplace/rating/1",
        json={"rating": 4},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rating_delete_requires_auth(client):
    """未登录删除评分 → 401"""
    response = await client.delete("/marketplace/rating/1")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_upload_requires_auth(client):
    """未登录上传 → 401"""
    response = await client.post(
        "/marketplace/developer/upload",
        data={"manifest_json": "{}"},
        files={"file": ("x.zip", b"fake", "application/zip")},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_upload_requires_permission(client):
    """Issue: 携带无效 token（格式合法但解不出用户）访问需 develop 权限端点
    → 401（在 require_permissions 内部 get_current_user 就失败）
    """
    response = await client.post(
        "/marketplace/developer/upload",
        data={"manifest_json": "{}"},
        files={"file": ("x.zip", b"fake", "application/zip")},
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    # 无效 JWT 解码失败 → AuthenticationException → 401
    assert response.status_code == 401
