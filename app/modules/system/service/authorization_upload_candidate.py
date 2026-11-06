"""Generic upload metadata is frozen and admitted by a complete FILE plan."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import BusinessRuleException, NotFoundException
from app.core.file_storage import validate_private_storage_roots
from app.core.id_generator import next_id
from app.core.rbac import is_super_admin
from app.modules.auth.service import load_live_user_authority
from app.modules.system.models.file import File
from app.modules.system.schemas.file import FileOut
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.file_policy_service import file_policy_service
from app.modules.system.service.file_service import (
    OPAQUE_MIME_TYPES,
    PRIVATE_BUSINESS_TYPES,
    PRIVATE_TEXT_MIME_BY_EXT,
    PUBLIC_IMAGE_TYPES,
    file_service,
)


def limit():
    return BusinessRuleException(
        "文件或批次数量超过同步预算", error_code="AUTHORIZATION_FILE_LIMIT_EXCEEDED"
    )


@dataclass(frozen=True, slots=True)
class UploadBytes:
    filename: str
    mime: str | None
    content: bytes
    business_type: str | None
    business_id: int | None


@dataclass(frozen=True, slots=True)
class PublishedUpload:
    file_id: int
    source: UploadBytes
    path: str
    url: str
    ext: str
    mime: str | None
    business_type: str | None


def scenario(filename, business_type):
    ext = Path(filename).suffix.lower()
    return (
        "image"
        if ext in PUBLIC_IMAGE_TYPES
        else "ai_file"
        if business_type == "ai-chat"
        else "import"
        if business_type == "user-import"
        else "general"
    )


def publish_uploads(sources, tenant_id, public_root, private_root):
    """Thread input contains immutable bytes/strings only, never ORM objects."""
    validate_private_storage_roots()
    public_root, private_root = (
        str(Path(public_root).resolve()),
        str(Path(private_root).resolve()),
    )
    now = datetime.now()
    results = []
    for source in sources:
        ext = Path(source.filename).suffix.lower()
        kind = file_service._normalize_business_type(ext, source.business_type)
        private = kind in PRIVATE_BUSINESS_TYPES
        mime = source.mime
        if not private or kind == "ai-chat-image":
            mime = file_service._validate_public_image(
                source.content, ext=ext, declared_mime=mime
            )
        elif kind == "ai-chat-private" and mime in OPAQUE_MIME_TYPES:
            mime = PRIVATE_TEXT_MIME_BY_EXT.get(ext, mime)
        identifier = next_id()
        relative = Path(
            f"tenant-{tenant_id}/{now.year}/{now.month:02d}/{now.day:02d}/{identifier}{ext}"
        )
        path = Path(private_root if private else public_root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(source.content)
        results.append(
            PublishedUpload(
                identifier,
                source,
                str(path),
                f"/uploads/{relative.as_posix()}"
                if not private or kind == "ai-chat-image"
                else "",
                ext,
                mime,
                kind,
            )
        )
    return tuple(results)


class UploadCandidate:
    async def policies(self, db, tx, *, tenant, actor_user_version, names):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("system:file:upload",),
        )
        await tx.lock(LockPlan(existing=identity.keys))
        await assert_identity(db, identity)
        return tuple(
            [
                await file_policy_service.resolve(
                    db, scenario(name, kind), tenant=tenant
                )
                for name, kind in names
            ]
        )

    async def insert(self, db, tx, *, tenant, actor_user_version, uploads):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("system:file:upload",),
        )
        keys = tuple(
            ResourceKey(K.FILE, tenant.tenant_id, item.file_id) for item in uploads
        )
        await tx.lock(
            LockPlan(
                existing=identity.keys, new=tuple(NewResource(key) for key in keys)
            )
        )
        await assert_identity(db, identity)
        for key, item in zip(keys, uploads, strict=True):
            policy = await file_policy_service.resolve(
                db,
                scenario(item.source.filename, item.source.business_type),
                tenant=tenant,
            )
            policy.validate_extension(item.source.filename)
            policy.validate_size(len(item.source.content))
            await tx.insert_reserved(
                key,
                {
                    "original_name": item.source.filename,
                    "file_name": str(item.file_id),
                    "file_path": item.path,
                    "file_url": item.url,
                    "file_size": len(item.source.content),
                    "file_ext": item.ext,
                    "mime_type": item.mime,
                    "business_type": item.business_type,
                    "business_id": item.source.business_id,
                    "owner_user_id": tenant.actor_user_id,
                    "create_by": identity.user_name,
                    "del_flag": "0",
                },
            )
        tx.seal_insertions()
        return tuple(
            [
                FileOut.model_validate(await db.get(File, item.file_id))
                for item in uploads
            ]
        )

    async def delete(self, db, tx, *, tenant, actor_user_version, identifiers, batch):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("system:file:delete",) if batch else (),
        )
        found = set(
            (
                await db.scalars(
                    select(File.file_id).where(
                        File.tenant_id == tenant.tenant_id,
                        File.file_id.in_(identifiers),
                        File.del_flag == "0",
                    )
                )
            ).all()
        )
        if found != set(identifiers):
            raise NotFoundException("文件")
        keys = tuple(
            ResourceKey(K.FILE, tenant.tenant_id, item) for item in identifiers
        )
        await tx.lock(LockPlan(existing=(*identity.keys, *keys)))
        await assert_identity(db, identity)
        actor = await load_live_user_authority(db, tenant=tenant)
        admin = batch or is_super_admin(actor)
        records = tuple(
            (
                await db.scalars(
                    select(File).where(
                        File.tenant_id == tenant.tenant_id,
                        File.file_id.in_(identifiers),
                        File.del_flag == "0",
                    )
                )
            ).all()
        )
        if {r.file_id for r in records} != set(identifiers) or any(
            not admin and r.owner_user_id != tenant.actor_user_id for r in records
        ):
            raise NotFoundException("文件")
        paths = tuple(record.file_path for record in records)
        for record in records:
            await tx.delete_file_fact(
                ResourceKey(K.FILE, tenant.tenant_id, record.file_id),
                expected={
                    "owner_user_id": record.owner_user_id,
                    "file_path": record.file_path,
                    "del_flag": "0",
                },
            )
        return paths


upload_candidate = UploadCandidate()
