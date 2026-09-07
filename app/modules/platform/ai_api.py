"""Dedicated platform control-plane APIs for global AI configuration."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import BusinessRuleException
from app.core.tenant import PlatformContext
from app.db.session import get_db
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.schemas.agent_admin import (
    AgentAdminDetailItem,
    AgentAdminListItem,
    AgentAdminUpdateReq,
)
from app.modules.ai.schemas.config_projection import redact_url
from app.modules.ai.schemas.model import ModelCreate, ModelOption, ModelOut, ModelUpdate
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderOut,
    ProviderQuery,
    ProviderTestRequest,
    ProviderUpdate,
)
from app.modules.ai.service.agent_admin import agent_admin_service
from app.modules.ai.service.model_service import model_service
from app.modules.ai.service.provider_service import provider_service
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service,
)
from app.modules.auth.service import require_platform_context
from app.modules.platform.schemas import (
    PlatformTenantModelPolicyOut,
    PlatformTenantModelPolicyPut,
)

router = APIRouter()
PositiveId = Annotated[int, Path(gt=0, le=9_223_372_036_854_775_807)]
TenantId = Annotated[int, Path(ge=0, le=9_223_372_036_854_775_807)]


def _record_count(request: Request, count: int) -> None:
    request.state.platform_result_summary = {"recordCount": count}


@router.get(
    "/ai/agents",
    response_model=ResponseModel[list[AgentAdminListItem]],
    summary="平台：列出全局 AI Agent",
)
async def list_agents(
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    items = await agent_admin_service.list_agents(db, platform=platform)
    _record_count(request, len(items))
    return ResponseModel.success(data=items)


@router.get(
    "/ai/agents/model-options",
    response_model=ResponseModel[list[ModelOption]],
    summary="平台：列出 Agent 可选模型",
)
async def list_agent_model_options(
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    items = await model_service.list_options(db, platform=platform)
    _record_count(request, len(items))
    return ResponseModel.success(data=items)


@router.get(
    "/ai/agents/{agent_id}",
    response_model=ResponseModel[AgentAdminDetailItem],
    summary="平台：读取全局 AI Agent",
)
async def get_agent(
    agent_id: PositiveId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    item = await agent_admin_service.get_agent(db, agent_id, platform=platform)
    _record_count(request, 1)
    return ResponseModel.success(data=item)


@router.put(
    "/ai/agents/{agent_id}",
    response_model=ResponseModel[AgentAdminDetailItem],
    summary="平台：更新全局 AI Agent",
)
async def update_agent(
    agent_id: PositiveId,
    payload: AgentAdminUpdateReq,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    item = await agent_admin_service.update_agent(
        db, agent_id, payload, platform=platform
    )
    _record_count(request, 1)
    return ResponseModel.success(data=item)


@router.get(
    "/ai/providers/models",
    summary="平台：列出启用的 Provider 模型目录",
)
async def list_available_models(
    request: Request,
    capability: str | None = None,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    rows = await model_service.list_available_with_provider(
        db, capability, platform=platform
    )
    items = []
    for model, provider in rows:
        allowed = await provider_egress.is_configuration_allowed(
            provider.provider_code,
            provider.base_url,
            model_base_url=model.base_url,
            configs=(provider.config, model.config),
        )
        items.append(
            {
                "modelId": str(model.model_id),
                "providerId": str(provider.provider_id),
                "providerCode": provider.provider_code,
                "providerName": provider.name,
                "model": model.name,
                "capabilities": list(model.capabilities or []),
                "baseUrl": redact_url(model.base_url or provider.base_url),
                "egressStatus": None if allowed else "EGRESS_POLICY_BLOCKED",
            }
        )
    _record_count(request, len(items))
    return ResponseModel.success(data=items)


@router.get(
    "/ai/providers",
    response_model=ResponseModel[PageResult[ProviderOut]],
    summary="平台：分页读取 AI Provider",
)
async def list_providers(
    request: Request,
    query: ProviderQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    page = await provider_service.get_list(db, query, platform=platform)
    _record_count(request, len(page.records))
    return ResponseModel.success(data=page)


@router.post(
    "/ai/providers",
    response_model=ResponseModel[ProviderOut],
    summary="平台：创建 AI Provider",
)
async def create_provider(
    payload: ProviderCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    provider = await provider_service.create(db, payload, platform=platform)
    await db.flush()
    await db.refresh(provider)
    data = ProviderOut.from_record(provider)
    _record_count(request, 1)
    return ResponseModel.success(data=data)


@router.put(
    "/ai/providers/{provider_id}",
    response_model=ResponseModel[ProviderOut],
    summary="平台：更新 AI Provider",
)
async def update_provider(
    provider_id: PositiveId,
    payload: ProviderUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    provider = await provider_service.update(
        db, provider_id, payload, platform=platform
    )
    await db.flush()
    await db.refresh(provider)
    data = ProviderOut.from_record(provider)
    _record_count(request, 1)
    return ResponseModel.success(data=data)


@router.delete("/ai/providers/{provider_id}", summary="平台：删除 AI Provider")
async def delete_provider(
    provider_id: PositiveId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    await provider_service.delete(db, provider_id, platform=platform)
    _record_count(request, 1)
    return ResponseModel.success()


@router.get(
    "/ai/providers/{provider_id}/models",
    response_model=ResponseModel[list[ModelOut]],
    summary="平台：列出 Provider 模型",
)
async def list_provider_models(
    provider_id: PositiveId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    provider = await provider_service.get_by_id(db, provider_id, platform=platform)
    models = await model_service.get_by_provider(db, provider_id, platform=platform)
    items: list[ModelOut] = []
    for model in models:
        allowed = await provider_egress.is_configuration_allowed(
            provider.provider_code,
            provider.base_url,
            model_base_url=model.base_url,
            configs=(provider.config, model.config),
        )
        items.append(
            ModelOut.model_validate(model).model_copy(
                update={"egress_status": None if allowed else "EGRESS_POLICY_BLOCKED"}
            )
        )
    _record_count(request, len(items))
    return ResponseModel.success(data=items)


@router.post(
    "/ai/providers/{provider_id}/models",
    response_model=ResponseModel[ModelOut],
    summary="平台：创建 Provider 模型",
)
async def create_model(
    provider_id: PositiveId,
    payload: ModelCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    model = await model_service.create(
        db,
        provider_id,
        payload,
        create_by=f"platform:{platform.actor_principal_id}",
        platform=platform,
    )
    await db.flush()
    await db.refresh(model)
    data = ModelOut.model_validate(model)
    _record_count(request, 1)
    return ResponseModel.success(data=data)


async def _require_provider_model(
    db: AsyncSession,
    *,
    provider_id: int,
    model_id: int,
    platform: PlatformContext,
):
    model = await model_service.get_by_id_for_write(db, model_id, platform=platform)
    if model.provider_id != provider_id:
        raise BusinessRuleException(
            "模型不属于指定 Provider",
            error_code="AI_PROVIDER_MODEL_MISMATCH",
        )
    return model


@router.put(
    "/ai/providers/{provider_id}/models/{model_id}",
    response_model=ResponseModel[ModelOut],
    summary="平台：更新 Provider 模型",
)
async def update_model(
    provider_id: PositiveId,
    model_id: PositiveId,
    payload: ModelUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    await _require_provider_model(
        db, provider_id=provider_id, model_id=model_id, platform=platform
    )
    model = await model_service.update(db, model_id, payload, platform=platform)
    await db.flush()
    await db.refresh(model)
    data = ModelOut.model_validate(model)
    _record_count(request, 1)
    return ResponseModel.success(data=data)


@router.delete(
    "/ai/providers/{provider_id}/models/{model_id}",
    summary="平台：删除 Provider 模型",
)
async def delete_model(
    provider_id: PositiveId,
    model_id: PositiveId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    await _require_provider_model(
        db, provider_id=provider_id, model_id=model_id, platform=platform
    )
    await model_service.delete(db, model_id, platform=platform)
    _record_count(request, 1)
    return ResponseModel.success()


@router.post(
    "/ai/providers/{provider_id}/test",
    summary="平台：测试已保存 Provider 模型",
)
async def test_provider_model(
    provider_id: PositiveId,
    payload: ProviderTestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    result = await provider_service.test_connection(
        db, provider_id, int(payload.model_id), platform=platform
    )
    _record_count(request, 1)
    return ResponseModel.success(data=result)


@router.get(
    "/tenants/{tenant_id}/ai/model-policies",
    response_model=ResponseModel[list[PlatformTenantModelPolicyOut]],
    summary="平台：列出租户 AI 模型策略",
)
async def list_tenant_model_policies(
    tenant_id: TenantId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    rows = await tenant_model_policy_admin_service.list(
        db, tenant_id=tenant_id, platform=platform
    )
    data = [PlatformTenantModelPolicyOut.from_projection(row) for row in rows]
    _record_count(request, len(data))
    return ResponseModel.success(data=data)


@router.put(
    "/tenants/{tenant_id}/ai/model-policies/{model_id}",
    response_model=ResponseModel[PlatformTenantModelPolicyOut],
    summary="平台：替换租户 AI 模型策略",
)
async def put_tenant_model_policy(
    tenant_id: TenantId,
    model_id: PositiveId,
    payload: PlatformTenantModelPolicyPut,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    row = await tenant_model_policy_admin_service.put(
        db,
        tenant_id=tenant_id,
        model_id=model_id,
        data=payload,
        platform=platform,
    )
    data = PlatformTenantModelPolicyOut.from_projection(row)
    _record_count(request, 1)
    return ResponseModel.success(data=data)


@router.delete(
    "/tenants/{tenant_id}/ai/model-policies/{model_id}",
    summary="平台：删除租户 AI 模型策略",
)
async def delete_tenant_model_policy(
    tenant_id: TenantId,
    model_id: PositiveId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    await tenant_model_policy_admin_service.delete(
        db, tenant_id=tenant_id, model_id=model_id, platform=platform
    )
    _record_count(request, 1)
    return ResponseModel.success()
