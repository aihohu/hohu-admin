"""Explicit policy control plane. Business field authorization never comes from UI."""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import ResponseModel
from app.db.authorization_http import get_authorization_runtime, release_request_reads
from app.db.session import get_db
from app.modules.auth.service import access_identity
from app.modules.system.models.user import User
from app.modules.system.schemas.user_field_policy import (
    PolicyDraft,
    PolicyPreviewResult,
    PolicyPublish,
    PolicyReadResult,
    PolicyResult,
    PolicyTransition,
)

router = APIRouter()


async def _call(db, actor, role_id, operation, payload=None):
    identity = access_identity(actor)
    await release_request_reads(db)
    result = await get_authorization_runtime().field_policy(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        role_id=role_id,
        operation=operation,
        payload=payload,
    )
    return ResponseModel.success(data=result)


@router.get("/{role_id}", response_model=ResponseModel[PolicyReadResult])
async def get_policy(
    role_id: int = Path(gt=0, le=2**63 - 1),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await _call(db, actor, role_id, "read")


@router.post("/{role_id}/preview", response_model=ResponseModel[PolicyPreviewResult])
async def preview_policy(
    payload: PolicyDraft,
    role_id: int = Path(gt=0, le=2**63 - 1),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await _call(db, actor, role_id, "preview", payload)


@router.post("/{role_id}/publish", response_model=ResponseModel[PolicyResult])
async def publish_policy(
    payload: PolicyPublish,
    role_id: int = Path(gt=0, le=2**63 - 1),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await _call(db, actor, role_id, "publish", payload)


@router.post("/{role_id}/ready", response_model=ResponseModel[PolicyResult])
async def ready_policy(
    payload: PolicyTransition,
    role_id: int = Path(gt=0, le=2**63 - 1),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await _call(db, actor, role_id, "ready", payload)


@router.post("/{role_id}/activate", response_model=ResponseModel[PolicyResult])
async def activate_policy(
    payload: PolicyTransition,
    role_id: int = Path(gt=0, le=2**63 - 1),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await _call(db, actor, role_id, "activate", payload)


@router.post("/{role_id}/freeze", response_model=ResponseModel[PolicyResult])
async def freeze_policy(
    payload: PolicyTransition,
    role_id: int = Path(gt=0, le=2**63 - 1),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await _call(db, actor, role_id, "freeze", payload)
