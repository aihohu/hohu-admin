from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.core.tenant import PlatformContext
from app.db.session import get_db
from app.modules.auth.service import require_platform_context
from app.modules.platform.schemas import (
    PlatformLoginCredentials,
    PlatformRetentionOut,
    PlatformRetentionPreviewRequest,
    PlatformRetentionPurgeRequest,
    PlatformSupportAuditOut,
    PlatformSupportAuditPage,
    PlatformSupportAuditQuery,
    PlatformTenantBootstrapOut,
    PlatformTenantBootstrapRequest,
    PlatformTenantCreate,
    PlatformTenantOut,
    PlatformTenantPage,
    PlatformTenantQuery,
    PlatformTokenResponse,
)
from app.modules.platform.service import platform_auth_service
from app.modules.platform.tenant_bootstrap_service import tenant_bootstrap_service
from app.modules.system.service.tenant_lifecycle_service import (
    tenant_lifecycle_service,
)
from app.modules.system.service.tenant_support_service import tenant_support_service

router = APIRouter()
control_router = APIRouter()
TenantId = Annotated[int, Path(ge=0, le=9_223_372_036_854_775_807)]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$",
    ),
]


@router.post("/login", summary="平台控制面登录")
async def platform_login(
    credentials: PlatformLoginCredentials,
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[PlatformTokenResponse]:
    token = await platform_auth_service.authenticate(db, credentials)
    await db.commit()
    return ResponseModel.success(data=PlatformTokenResponse(token=token))


@control_router.post(
    "/tenants",
    response_model=ResponseModel[PlatformTenantOut],
    summary="准备不可登录的租户注册记录",
)
async def prepare_tenant(
    payload: PlatformTenantCreate,
    idempotency_key: IdempotencyKey,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    tenant_id = platform.target_tenant_id
    if tenant_id is None:  # defensive: dependency always preallocates this target
        raise RuntimeError("platform tenant target was not allocated")
    tenant = await tenant_lifecycle_service.prepare_tenant(
        db,
        tenant_id=tenant_id,
        tenant_code=payload.tenant_code,
        tenant_name=payload.tenant_name,
        idempotency_key=idempotency_key,
        platform=platform,
    )
    await db.commit()
    request.state.platform_result_summary = {"recordCount": 1}
    return ResponseModel.success(data=PlatformTenantOut.from_record(tenant))


@control_router.get(
    "/tenants",
    response_model=ResponseModel[PlatformTenantPage],
    summary="分页读取平台租户注册表",
)
async def list_tenants(
    request: Request,
    query: PlatformTenantQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    page = await tenant_lifecycle_service.list_tenants(
        db,
        current=query.current,
        size=query.size,
        platform=platform,
    )
    data = PlatformTenantPage(
        records=[PlatformTenantOut.from_record(record) for record in page.records],
        total=page.total,
        current=page.current,
        size=page.size,
    )
    request.state.platform_result_summary = {"recordCount": len(data.records)}
    return ResponseModel.success(data=data)


@control_router.get(
    "/tenants/{tenant_id}",
    response_model=ResponseModel[PlatformTenantOut],
    summary="读取租户安全摘要",
)
async def get_tenant(
    tenant_id: TenantId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    tenant = await tenant_lifecycle_service.get_tenant(
        db, tenant_id=tenant_id, platform=platform
    )
    request.state.platform_result_summary = {"recordCount": 1}
    return ResponseModel.success(data=PlatformTenantOut.from_record(tenant))


@control_router.post(
    "/tenants/{tenant_id}/disable",
    response_model=ResponseModel[PlatformTenantOut],
    summary="禁用非默认租户",
)
async def disable_tenant(
    tenant_id: TenantId,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    tenant = await tenant_lifecycle_service.disable_tenant(
        db, tenant_id=tenant_id, platform=platform
    )
    await db.commit()
    request.state.platform_result_summary = {"recordCount": 1}
    return ResponseModel.success(data=PlatformTenantOut.from_record(tenant))


@control_router.post(
    "/tenants/{tenant_id}/bootstrap",
    response_model=ResponseModel[PlatformTenantBootstrapOut],
    summary="原子引导 prepared tenant",
)
async def bootstrap_tenant(
    tenant_id: TenantId,
    payload: PlatformTenantBootstrapRequest,
    idempotency_key: IdempotencyKey,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    result = await tenant_bootstrap_service.bootstrap(
        db,
        tenant_id=tenant_id,
        default_model_id=int(payload.default_model_id),
        admin_password=payload.admin_password.get_secret_value(),
        idempotency_key=idempotency_key,
        platform=platform,
    )
    await db.commit()
    request.state.platform_result_summary = {"recordCount": 1}
    return ResponseModel.success(data=PlatformTenantBootstrapOut.from_result(result))


def _support_page(page) -> PlatformSupportAuditPage:
    return PlatformSupportAuditPage(
        records=[
            PlatformSupportAuditOut.model_validate(record) for record in page.records
        ],
        total=page.total,
        current=page.current,
        size=page.size,
    )


@control_router.get(
    "/tenants/{tenant_id}/support/operation-logs",
    response_model=ResponseModel[PlatformSupportAuditPage],
    summary="读取脱敏操作审计时间线",
)
async def list_tenant_operation_logs(
    tenant_id: TenantId,
    request: Request,
    query: PlatformSupportAuditQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    page = await tenant_support_service.list_operation_logs(
        db,
        tenant_id=tenant_id,
        current=query.current,
        size=query.size,
        platform=platform,
    )
    data = _support_page(page)
    request.state.platform_result_summary = {"recordCount": len(data.records)}
    return ResponseModel.success(data=data)


@control_router.get(
    "/tenants/{tenant_id}/support/login-logs",
    response_model=ResponseModel[PlatformSupportAuditPage],
    summary="读取脱敏登录审计时间线",
)
async def list_tenant_login_logs(
    tenant_id: TenantId,
    request: Request,
    query: PlatformSupportAuditQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    page = await tenant_support_service.list_login_logs(
        db,
        tenant_id=tenant_id,
        current=query.current,
        size=query.size,
        platform=platform,
    )
    data = _support_page(page)
    request.state.platform_result_summary = {"recordCount": len(data.records)}
    return ResponseModel.success(data=data)


@control_router.post(
    "/tenants/{tenant_id}/audit-retention/preview",
    response_model=ResponseModel[PlatformRetentionOut],
    summary="预览租户 System 审计 retention",
)
async def preview_tenant_audit_retention(
    tenant_id: TenantId,
    payload: PlatformRetentionPreviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    result = await tenant_support_service.preview_retention(
        db,
        tenant_id=tenant_id,
        cutoff=payload.cutoff,
        platform=platform,
    )
    request.state.platform_result_summary = {"recordCount": result.affected_count}
    return ResponseModel.success(data=PlatformRetentionOut.from_result(result))


@control_router.post(
    "/tenants/{tenant_id}/audit-retention/purge",
    response_model=ResponseModel[PlatformRetentionOut],
    summary="执行租户 System 审计 retention",
)
async def purge_tenant_audit_retention(
    tenant_id: TenantId,
    payload: PlatformRetentionPurgeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    platform: PlatformContext = Depends(require_platform_context),
):
    result = await tenant_support_service.purge_retention(
        db,
        tenant_id=tenant_id,
        cutoff=payload.cutoff,
        expected_operation_count=payload.expected_operation_count,
        expected_login_count=payload.expected_login_count,
        platform=platform,
    )
    await db.commit()
    request.state.platform_result_summary = {"affectedCount": result.affected_count}
    return ResponseModel.success(data=PlatformRetentionOut.from_result(result))
