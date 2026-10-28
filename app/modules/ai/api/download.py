"""Short-lived AI result downloads with live projection authorization."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.exceptions import AuthorizationException, NotFoundException
from app.core.tenant import TenantContext
from app.db import authorization_http
from app.db.authorization_plan_session import _order_error
from app.db.session import get_db
from app.modules.auth.service import access_identity, get_current_tenant_context
from app.modules.system.models.user import User

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
    token: str = Query(..., min_length=1, max_length=16384),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """Re-authorize the signed projection before every file read."""
    identity = access_identity(current_user)
    if identity.tenant != tenant:
        raise _order_error()
    await authorization_http.release_request_reads(db)
    try:
        (
            xlsx_bytes,
            filename,
        ) = await authorization_http.get_authorization_runtime().transfer.download(
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            kind="export",
            identifier=export_id,
            token=token,
            signed=True,
        )
    except (NotFoundException, AuthorizationException) as exc:
        raise _download_not_found() from exc
    return Response(
        content=xlsx_bytes,
        media_type=_EXPORT_MIME_TYPE,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
