"""Dedicated platform control-plane APIs for global AI configuration."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import PlatformContext
from app.db.authorization_control_http import ControlHttp, require_control_write
from app.db.session import get_platform_db
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
    ProviderModelTestDraftRequest,
    ProviderModelTestDraftResult,
    ProviderOut,
    ProviderQuery,
    ProviderTestRequest,
    ProviderTestResult,
    ProviderUpdate,
)
from app.modules.ai.service.agent_admin import agent_admin_service
from app.modules.ai.service.model_service import model_service
from app.modules.ai.service.provider_service import provider_service
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service,
)
from app.modules.platform.schemas import (
    PlatformTenantModelCatalogOut,
    PlatformTenantModelPoliciesPut,
    PlatformTenantModelPolicyOut,
    PlatformTenantModelPolicyPut,
)
from app.modules.platform.system_agent_auth import require_system_agent_context

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
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
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
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
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
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
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
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(
        data=await control.execute(payload, target_id=agent_id)
    )


@router.get(
    "/ai/providers/models",
    summary="平台：列出启用的 Provider 模型目录",
)
async def list_available_models(
    request: Request,
    capability: str | None = None,
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
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
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
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
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute(payload))


@router.put(
    "/ai/providers/{provider_id}",
    response_model=ResponseModel[ProviderOut],
    summary="平台：更新 AI Provider",
)
async def update_provider(
    provider_id: PositiveId,
    payload: ProviderUpdate,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(
        data=await control.execute(payload, target_id=provider_id)
    )


@router.delete("/ai/providers/{provider_id}", summary="平台：删除 AI Provider")
async def delete_provider(
    provider_id: PositiveId,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    await control.execute(target_id=provider_id)
    return ResponseModel.success()


@router.get(
    "/ai/providers/{provider_id}/models",
    response_model=ResponseModel[list[ModelOut]],
    summary="平台：列出 Provider 模型",
)
async def list_provider_models(
    provider_id: PositiveId,
    request: Request,
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
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
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(
        data=await control.execute(payload, provider_id=provider_id)
    )


@router.put(
    "/ai/providers/{provider_id}/models/{model_id}",
    response_model=ResponseModel[ModelOut],
    summary="平台：更新 Provider 模型",
)
async def update_model(
    provider_id: PositiveId,
    model_id: PositiveId,
    payload: ModelUpdate,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(
        data=await control.execute(payload, target_id=model_id, provider_id=provider_id)
    )


@router.delete(
    "/ai/providers/{provider_id}/models/{model_id}",
    summary="平台：删除 Provider 模型",
)
async def delete_model(
    provider_id: PositiveId,
    model_id: PositiveId,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    await control.execute(target_id=model_id, provider_id=provider_id)
    return ResponseModel.success()


@router.post(
    "/ai/providers/test",
    response_model=ResponseModel[ProviderModelTestDraftResult],
    summary="平台：测试当前 Provider 与模型表单",
)
async def test_provider_model_draft(
    payload: ProviderModelTestDraftRequest,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    await control.execute(payload)
    return ResponseModel.success(data=ProviderModelTestDraftResult())


@router.post(
    "/ai/providers/{provider_id}/test",
    summary="平台：测试已保存 Provider 模型",
)
async def test_provider_model(
    provider_id: PositiveId,
    payload: ProviderTestRequest,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    await control.execute(model_id=int(payload.model_id), provider_id=provider_id)
    return ResponseModel.success(
        data=ProviderTestResult(provider_id=provider_id, model_id=int(payload.model_id))
    )


@router.get(
    "/tenants/{tenant_id}/ai/model-policies",
    response_model=ResponseModel[list[PlatformTenantModelPolicyOut]],
    summary="平台：列出租户 AI 模型策略",
)
async def list_tenant_model_policies(
    tenant_id: TenantId,
    request: Request,
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
):
    rows = await tenant_model_policy_admin_service.list(
        db, tenant_id=tenant_id, platform=platform
    )
    data = [PlatformTenantModelPolicyOut.from_projection(row) for row in rows]
    _record_count(request, len(data))
    return ResponseModel.success(data=data)


@router.get(
    "/tenants/{tenant_id}/ai/model-policies/catalog",
    response_model=ResponseModel[PlatformTenantModelCatalogOut],
    summary="平台：租户 AI 授权模型目录",
)
async def tenant_model_catalog(
    tenant_id: TenantId,
    request: Request,
    db: AsyncSession = Depends(get_platform_db, scope="function"),
    platform: PlatformContext = Depends(require_system_agent_context, scope="function"),
):
    result = await tenant_model_policy_admin_service.catalog(
        db, tenant_id=tenant_id, platform=platform
    )
    _record_count(request, len(result.models))
    return ResponseModel.success(
        data=PlatformTenantModelCatalogOut.from_projection(result)
    )


@router.put(
    "/tenants/{tenant_id}/ai/model-policies",
    response_model=ResponseModel[PlatformTenantModelCatalogOut],
    summary="平台：原子保存租户 AI 模型授权",
)
async def put_tenant_model_policies(
    tenant_id: TenantId,
    payload: PlatformTenantModelPoliciesPut,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(
        data=await control.execute(payload, target_tenant_id=tenant_id)
    )


@router.put(
    "/tenants/{tenant_id}/ai/model-policies/{model_id}",
    response_model=ResponseModel[PlatformTenantModelPolicyOut],
    summary="平台：替换租户 AI 模型策略",
)
async def put_tenant_model_policy(
    tenant_id: TenantId,
    model_id: PositiveId,
    payload: PlatformTenantModelPolicyPut,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(
        data=await control.execute(
            payload, target_id=model_id, target_tenant_id=tenant_id
        )
    )


@router.delete(
    "/tenants/{tenant_id}/ai/model-policies/{model_id}",
    summary="平台：删除租户 AI 模型策略",
)
async def delete_tenant_model_policy(
    tenant_id: TenantId,
    model_id: PositiveId,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    await control.execute(target_id=model_id, target_tenant_id=tenant_id)
    return ResponseModel.success()
