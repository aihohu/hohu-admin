from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import PlatformContext
from app.db.session import get_db
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.schemas.model import ModelCreate, ModelOut, ModelUpdate
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderOut,
    ProviderQuery,
    ProviderTestRequest,
    ProviderUpdate,
)
from app.modules.ai.service.model_service import model_service
from app.modules.ai.service.provider_service import provider_service
from app.modules.auth.service import require_platform_context

router = APIRouter()


@router.get(
    "/models",
    summary="获取 Provider 管理可用模型列表",
)
async def get_available_models(
    capability: str | None = None,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    """返回所有启用的模型列表，供对话选择

    查询参数:
    - capability: 可选，按能力过滤（text / vision / image-gen）
    """
    rows = await model_service.list_available_with_provider(
        db, capability, platform=platform
    )

    models = []
    for model, provider in rows:
        caps = model.capabilities or []
        allowed = await provider_egress.is_configuration_allowed(
            provider.provider_code,
            provider.base_url,
            model_base_url=model.base_url,
            configs=(provider.config, model.config),
        )
        models.append(
            {
                "modelId": str(model.model_id),
                "providerId": str(provider.provider_id),
                "providerCode": provider.provider_code,
                "providerName": provider.name,
                "model": model.name,
                "capabilities": caps,
                "baseUrl": model.base_url or provider.base_url,
                "egressStatus": None if allowed else "EGRESS_POLICY_BLOCKED",
            }
        )
    return ResponseModel.success(data=models)


@router.get(
    "/list",
    summary="获取提供商列表",
    response_model=ResponseModel[PageResult[ProviderOut]],
)
async def get_provider_list(
    query: ProviderQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    page_data = await provider_service.get_list(db, query, platform=platform)
    return ResponseModel.success(data=page_data)


@router.post(
    "/add",
    summary="添加提供商",
)
async def add_provider(
    data: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    obj = await provider_service.create(db, data, platform=platform)
    await db.commit()
    return ResponseModel.success(data=ProviderOut.model_validate(obj), msg="添加成功")


@router.put(
    "/{provider_id}",
    summary="更新提供商",
)
async def update_provider(
    provider_id: int,
    data: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    await provider_service.update(db, provider_id, data, platform=platform)
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete(
    "/{provider_id}",
    summary="删除提供商",
)
async def delete_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    await provider_service.delete(db, provider_id, platform=platform)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


# ── 模型管理（嵌套在提供商下） ──


@router.get(
    "/{provider_id}/models",
    summary="获取提供商下的模型列表",
)
async def get_provider_models(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    provider = await provider_service.get_by_id(db, provider_id, platform=platform)
    models = await model_service.get_by_provider(db, provider_id, platform=platform)
    outputs = []
    for model in models:
        allowed = await provider_egress.is_configuration_allowed(
            provider.provider_code,
            provider.base_url,
            model_base_url=model.base_url,
            configs=(provider.config, model.config),
        )
        outputs.append(
            ModelOut.model_validate(model).model_copy(
                update={"egress_status": None if allowed else "EGRESS_POLICY_BLOCKED"}
            )
        )
    return ResponseModel.success(data=outputs)


@router.post(
    "/{provider_id}/models",
    summary="添加模型",
)
async def add_model(
    provider_id: int,
    data: ModelCreate,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    await model_service.create(
        db,
        provider_id,
        data,
        create_by=f"platform:{platform.actor_principal_id}",
        platform=platform,
    )
    await db.commit()
    return ResponseModel.success(msg="添加成功")


@router.put(
    "/{provider_id}/models/{model_id}",
    summary="更新模型",
)
async def update_model(
    provider_id: int,
    model_id: int,
    data: ModelUpdate,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    model = await model_service.get_by_id_for_write(db, model_id, platform=platform)
    if model.provider_id != provider_id:
        return ResponseModel.error(msg="模型不属于该提供商", code=400)
    await model_service.update(db, model_id, data, platform=platform)
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete(
    "/{provider_id}/models/{model_id}",
    summary="删除模型",
)
async def delete_model(
    provider_id: int,
    model_id: int,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    model = await model_service.get_by_id_for_write(db, model_id, platform=platform)
    if model.provider_id != provider_id:
        return ResponseModel.error(msg="模型不属于该提供商", code=400)
    await model_service.delete(db, model_id, platform=platform)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


@router.post(
    "/{provider_id}/test",
    summary="测试模型连通性",
)
async def test_model(
    provider_id: int,
    data: ProviderTestRequest,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    result = await provider_service.test_connection(
        db,
        provider_id,
        int(data.model_id),
        platform=platform,
    )
    return ResponseModel.success(msg="连通性测试成功", data=result)
