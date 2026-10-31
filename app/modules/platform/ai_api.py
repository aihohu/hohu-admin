"""Dedicated platform control-plane APIs for global AI configuration."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.core.base_response import PageResult, ResponseModel
from app.db.authorization_control_http import ControlHttp, require_control_write
from app.modules.ai.schemas.agent_admin import (
    AgentAdminDetailItem,
    AgentAdminListItem,
    AgentAdminUpdateReq,
)
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
from app.modules.platform.schemas import (
    PlatformTenantModelCatalogOut,
    PlatformTenantModelPoliciesPut,
    PlatformTenantModelPolicyOut,
    PlatformTenantModelPolicyPut,
)

router = APIRouter()
PositiveId = Annotated[int, Path(gt=0, le=9_223_372_036_854_775_807)]
TenantId = Annotated[int, Path(ge=0, le=9_223_372_036_854_775_807)]


@router.get(
    "/ai/agents",
    response_model=ResponseModel[list[AgentAdminListItem]],
    summary="平台：列出全局 AI Agent",
)
async def list_agents(
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute())


@router.get(
    "/ai/agents/model-options",
    response_model=ResponseModel[list[ModelOption]],
    summary="平台：列出 Agent 可选模型",
)
async def list_agent_model_options(
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute())


@router.get(
    "/ai/agents/{agent_id}",
    response_model=ResponseModel[AgentAdminDetailItem],
    summary="平台：读取全局 AI Agent",
)
async def get_agent(
    agent_id: PositiveId,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute(target_id=agent_id))


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
    capability: str | None = None,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute({"capability": capability}))


@router.get(
    "/ai/providers",
    response_model=ResponseModel[PageResult[ProviderOut]],
    summary="平台：分页读取 AI Provider",
)
async def list_providers(
    query: ProviderQuery = Depends(),
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute(query))


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
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute(provider_id=provider_id))


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
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute(target_tenant_id=tenant_id))


@router.get(
    "/tenants/{tenant_id}/ai/model-policies/catalog",
    response_model=ResponseModel[PlatformTenantModelCatalogOut],
    summary="平台：租户 AI 授权模型目录",
)
async def tenant_model_catalog(
    tenant_id: TenantId,
    control: ControlHttp = Depends(require_control_write, scope="function"),
):
    return ResponseModel.success(data=await control.execute(target_tenant_id=tenant_id))


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
