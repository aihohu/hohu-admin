"""Short-lived AI result downloads with live projection authorization."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.exceptions import NotFoundException
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)
from app.modules.auth.service import get_current_tenant_context
from app.modules.system.models.user import User
from app.modules.system.service.user_export_service import download_export_file

router = APIRouter()

_EXPORT_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _download_not_found() -> NotFoundException:
    return NotFoundException(
        "AI result download",
        error_code="AI_RESULT_DOWNLOAD_NOT_FOUND",
    )


@router.get("/user-export/{export_id}", summary="Download an AI user export")
async def download_ai_user_export(
    export_id: str,
    token: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """Re-authorize the signed projection before every file read."""
    lineage = result_projection_service.read_download_token(
        token,
        current_user,
        tenant=tenant,
        resource_type="user_export",
        resource_id=export_id,
    )
    if lineage is None:
        raise _download_not_found()
    allowed = await result_projection_service.authorize_result_projection(
        db,
        current_user,
        owner_user_id=current_user.user_id,
        lineage=lineage,
    )
    if not allowed:
        raise _download_not_found()
    try:
        xlsx_bytes, filename = await download_export_file(
            db,
            export_id,
            operator_id=current_user.user_id,
            allow_cross_owner=False,
            tenant=tenant,
        )
    except NotFoundException as exc:
        raise _download_not_found() from exc
    return Response(
        content=xlsx_bytes,
        media_type=_EXPORT_MIME_TYPE,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
