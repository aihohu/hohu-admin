"""Immutable chat image metadata, revalidated around bounded disk work."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import BusinessRuleException
from app.core.file_storage import validate_private_storage_roots
from app.modules.system.models.file import File
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.file_policy_service import SCENARIO_CAPS
from app.modules.system.service.file_service import (
    PRIVATE_BUSINESS_TYPES,
    PUBLIC_IMAGE_TYPES,
    file_service,
)


def unavailable():
    return BusinessRuleException(
        "图片已失效或无权访问，请重新上传图片", error_code="AI_IMAGE_NOT_AVAILABLE"
    )


@dataclass(frozen=True, slots=True)
class ChatImage:
    tenant_id: int
    file_id: int
    owner_user_id: int
    file_url: str
    file_path: str
    file_ext: str
    mime_type: str | None
    business_type: str | None
    del_flag: str
    file_size: int


def image_snapshot(row):
    return ChatImage(
        **{name: getattr(row, name) for name in ChatImage.__dataclass_fields__}
    )


def read_image(snapshot, public_root, private_root):
    """No session or ORM object can reach the worker."""
    if type(snapshot) is not ChatImage:
        raise unavailable()
    try:
        validate_private_storage_roots()
        root = Path(
            private_root if snapshot.business_type == "ai-chat-image" else public_root
        ).resolve()
        path = Path(snapshot.file_path).resolve()
        if (
            not path.is_relative_to(root)
            or snapshot.file_ext not in PUBLIC_IMAGE_TYPES
            or snapshot.business_type in PRIVATE_BUSINESS_TYPES - {"ai-chat-image"}
        ):
            raise ValueError("invalid uploaded image")
        with path.open("rb") as stream:
            content = stream.read(SCENARIO_CAPS["image"] + 1)
        if len(content) > SCENARIO_CAPS["image"]:
            raise ValueError("oversized uploaded image")
        mime = file_service._validate_public_image(
            content, ext=snapshot.file_ext, declared_mime=snapshot.mime_type
        )
        return content, mime
    except (OSError, ValueError, BusinessRuleException):
        raise unavailable() from None


class ChatImageCandidate:
    async def image(
        self, db, tx, *, tenant, actor_user_version, file_url, expected=None
    ):
        if type(file_url) is not str or not 1 <= len(file_url) <= 2048:
            raise unavailable()
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        statement = (
            select(File)
            .where(
                File.tenant_id == tenant.tenant_id,
                File.file_url == file_url,
            )
            .order_by(File.file_id)
            .limit(2)
            .execution_options(populate_existing=True)
        )
        rows = list(await db.scalars(statement))
        if len(rows) != 1:
            raise unavailable()
        snapshot = image_snapshot(rows[0])
        if (
            snapshot.owner_user_id != tenant.actor_user_id
            or snapshot.del_flag != "0"
            or expected is not None
            and (type(expected) is not ChatImage or snapshot != expected)
        ):
            raise unavailable()
        key = ResourceKey(K.FILE, tenant.tenant_id, snapshot.file_id)
        await tx.lock(LockPlan(existing=(*identity.keys, key)))
        await assert_identity(db, identity)
        fresh = list(await db.scalars(statement))
        if len(fresh) != 1 or image_snapshot(fresh[0]) != snapshot:
            raise unavailable()
        return snapshot


chat_image_candidate = ChatImageCandidate()
