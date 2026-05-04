from fastapi import APIRouter, Depends
from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.core.security import decrypt_value
from app.db.session import get_db
from app.modules.ai.core.provider_registry import create_model
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderOut,
    ProviderQuery,
    ProviderUpdate,
)
from app.modules.ai.service.provider_service import provider_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

router = APIRouter()


@router.get("/models", summary="获取可用模型列表")
async def get_available_models(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """返回所有启用的提供商及其模型列表，供对话选择

    config 格式:
    { "models": ["gpt-4o", "gpt-4o-mini"] }
    兼容旧格式 config.default_model — 自动转为单元素列表
    """
    providers = await provider_service.get_all_enabled(db)
    result = []
    for p in providers:
        config = p.config or {}
        models_list = config.get("models", [])

        provider_models = []
        for m in models_list:
            model_code = m if isinstance(m, str) else m.get("model", "")
            if not model_code:
                continue
            provider_models.append(
                {
                    "providerId": str(p.provider_id),
                    "providerCode": p.provider_code,
                    "providerName": p.name,
                    "model": model_code,
                    "modelId": f"{p.provider_code}:{model_code}",
                }
            )
        result.extend(provider_models)
    return ResponseModel.success(data=result)


@router.get(
    "/list",
    summary="获取提供商列表",
    response_model=ResponseModel[PageResult[ProviderOut]],
)
async def get_provider_list(
    query: ProviderQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    page_data = await provider_service.get_list(db, query)
    return ResponseModel.success(data=page_data)


@router.post("/add", summary="添加提供商")
async def add_provider(
    data: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await provider_service.create(db, data)
    await db.commit()
    return ResponseModel.success(msg="添加成功")


@router.put("/{provider_id}", summary="更新提供商")
async def update_provider(
    provider_id: int,
    data: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await provider_service.update(db, provider_id, data)
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete("/{provider_id}", summary="删除提供商")
async def delete_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await provider_service.delete(db, provider_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


@router.post("/test-model", summary="测试模型连通性")
async def test_model(
    data: dict,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """测试指定模型的连通性，支持未保存的提供商配置"""
    provider_code = data.get("providerCode", "")
    model_name = data.get("model", "")
    api_key_raw = data.get("apiKey", "")
    base_url = data.get("baseUrl") or None
    provider_id = data.get("providerId")

    if not model_name:
        return ResponseModel.error(msg="请输入模型名称", code=400)

    # 如果有 providerId 且没传 apiKey，从数据库读取解密后的 key
    if provider_id and not api_key_raw:
        try:
            provider = await provider_service.get_by_id(db, int(provider_id))
            api_key = decrypt_value(provider.api_key)
            if not base_url:
                base_url = provider.base_url
            if not provider_code:
                provider_code = provider.provider_code
        except Exception:
            return ResponseModel.error(msg="提供商不存在", code=404)
    else:
        api_key = api_key_raw

    if not api_key:
        return ResponseModel.error(
            msg="缺少 API Key，请填写或选择已保存的提供商", code=400
        )

    try:
        model = create_model(provider_code, model_name, api_key, base_url)
        test_agent = Agent(model, instructions="Reply with OK")
        result = await test_agent.run("Say OK")
        return ResponseModel.success(
            msg="连通性测试成功", data={"response": result.output}
        )
    except Exception as e:
        return ResponseModel.error(msg=f"连通性测试失败: {e}", code=500)
