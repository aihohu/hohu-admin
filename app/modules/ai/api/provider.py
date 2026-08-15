from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import DEFAULT_TENANT_ID
from app.db.session import get_db
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
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
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/models",
    summary="获取 Provider 管理可用模型列表",
    dependencies=[Depends(require_permissions("ai:provider:list"))],
)
async def get_available_models(
    capability: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """返回所有启用的模型列表，供对话选择

    查询参数:
    - capability: 可选，按能力过滤（text / vision / image-gen）
    """
    stmt = (
        select(AiModel, AiProvider)
        .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
        .where(AiModel.is_enabled.is_(True), AiProvider.is_enabled.is_(True))
        .order_by(AiModel.sort_order, AiModel.model_id)
    )
    result = await db.execute(stmt)
    rows = result.all()

    models = []
    for model, provider in rows:
        caps = model.capabilities or []
        if capability and capability not in caps:
            continue
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
    dependencies=[Depends(require_permissions("ai:provider:list"))],
)
async def get_provider_list(
    query: ProviderQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    page_data = await provider_service.get_list(db, query)
    return ResponseModel.success(data=page_data)


@router.post(
    "/add",
    summary="添加提供商",
    dependencies=[Depends(require_permissions("ai:provider:add"))],
)
async def add_provider(
    data: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    obj = await provider_service.create(db, data)
    await db.commit()
    return ResponseModel.success(data=ProviderOut.model_validate(obj), msg="添加成功")


@router.put(
    "/{provider_id}",
    summary="更新提供商",
    dependencies=[Depends(require_permissions("ai:provider:edit"))],
)
async def update_provider(
    provider_id: int,
    data: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await provider_service.update(db, provider_id, data)
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete(
    "/{provider_id}",
    summary="删除提供商",
    dependencies=[Depends(require_permissions("ai:provider:delete"))],
)
async def delete_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await provider_service.delete(db, provider_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


# ── 模型管理（嵌套在提供商下） ──


@router.get(
    "/{provider_id}/models",
    summary="获取提供商下的模型列表",
    dependencies=[Depends(require_permissions("ai:provider:list"))],
)
async def get_provider_models(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    provider = await provider_service.get_by_id(db, provider_id)
    models = await model_service.get_by_provider(db, provider_id)
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
    dependencies=[Depends(require_permissions("ai:provider:add"))],
)
async def add_model(
    provider_id: int,
    data: ModelCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await provider_service.get_by_id(db, provider_id)
    await model_service.create(db, provider_id, data, create_by=current_user.user_name)
    await db.commit()
    return ResponseModel.success(msg="添加成功")


@router.put(
    "/{provider_id}/models/{model_id}",
    summary="更新模型",
    dependencies=[Depends(require_permissions("ai:provider:edit"))],
)
async def update_model(
    provider_id: int,
    model_id: int,
    data: ModelUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    model = await model_service.get_by_id(db, model_id)
    if model.provider_id != provider_id:
        return ResponseModel.error(msg="模型不属于该提供商", code=400)
    await model_service.update(db, model_id, data)
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete(
    "/{provider_id}/models/{model_id}",
    summary="删除模型",
    dependencies=[Depends(require_permissions("ai:provider:delete"))],
)
async def delete_model(
    provider_id: int,
    model_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    model = await model_service.get_by_id(db, model_id)
    if model.provider_id != provider_id:
        return ResponseModel.error(msg="模型不属于该提供商", code=400)
    await model_service.delete(db, model_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


@router.post(
    "/{provider_id}/test",
    summary="测试模型连通性",
    dependencies=[Depends(require_permissions("ai:provider:test-model"))],
)
async def test_model(
    provider_id: int,
    data: ProviderTestRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    result = await provider_service.test_connection(
        db,
        provider_id,
        int(data.model_id),
        tenant_id=DEFAULT_TENANT_ID,
    )
    return ResponseModel.success(msg="连通性测试成功", data=result)
