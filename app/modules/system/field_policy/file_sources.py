"""Closed upload provenance and bounded detached file reads."""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from app.core.exceptions import BusinessRuleException

DELIVERY_CLASSES = frozenset(
    {"owned-private", "public-image", "ai-chat-private", "ai-chat-image", "user-import"}
)
PUBLIC_TYPES = frozenset({"avatar", "brand"})


def unavailable():
    return BusinessRuleException(
        "文件来源已失效或无权交付，请重新上传", error_code="FILE_SOURCE_UNAVAILABLE"
    )


def upload_delivery(extension, business_type, business_id):
    if business_id is not None:
        raise BusinessRuleException(
            "业务关联需要已登记的来源协议", error_code="FILE_BUSINESS_SOURCE_REQUIRED"
        )
    if business_type in PUBLIC_TYPES:
        if extension not in {".jpg", ".jpeg", ".png"}:
            raise BusinessRuleException(
                "公开资源只支持已验证的图片", error_code="FILE_PUBLIC_TYPE_DENIED"
            )
        return "public-image"
    if business_type in {"ai-chat-private", "ai-chat-image", "user-import"}:
        return business_type
    return "owned-private"


def make_file_manifest(*, tenant_id, owner_id, file_id, delivery_class, digest):
    if (
        type(tenant_id) is not int
        or not 0 <= tenant_id < 2**63
        or any(
            type(value) is not int or not 0 < value < 2**63
            for value in (owner_id, file_id)
        )
        or type(delivery_class) is not str
        or delivery_class not in DELIVERY_CLASSES
        or type(digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise unavailable()
    return {
        "schema": "file-source/1",
        "tenant_id": str(tenant_id),
        "owner_id": str(owner_id),
        "file_id": str(file_id),
        "delivery_class": delivery_class,
        "sha256": digest,
    }


@dataclass(frozen=True, slots=True, repr=False)
class FileContentSnapshot:
    tenant_id: int
    file_id: int
    owner_id: int
    path: str
    extension: str
    mime_type: str | None
    size: int
    delivery_class: str
    digest: str | None
    manifest: object

    def __post_init__(self):
        if type(self.manifest) is dict:
            object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))


def require_file_manifest(snapshot):
    if type(snapshot) is not FileContentSnapshot:
        raise unavailable()
    expected = make_file_manifest(
        tenant_id=snapshot.tenant_id,
        owner_id=snapshot.owner_id,
        file_id=snapshot.file_id,
        delivery_class=snapshot.delivery_class,
        digest=snapshot.digest,
    )
    if snapshot.manifest != expected:
        raise unavailable()
    return snapshot


def read_file_content(snapshot, public_root, private_root):
    """Input is a frozen scalar snapshot; no session crosses this boundary."""
    require_file_manifest(snapshot)
    cap = (20 if snapshot.extension in {".jpg", ".jpeg", ".png"} else 100) * 1024 * 1024
    if type(snapshot.size) is not int or not 0 <= snapshot.size <= cap:
        raise unavailable()
    try:
        root = Path(
            public_root if snapshot.delivery_class == "public-image" else private_root
        ).resolve()
        original = Path(snapshot.path)
        path = original.resolve()
        if (
            not path.is_relative_to(root)
            or path == root
            or any(
                part.is_symlink()
                or (hasattr(part, "is_junction") and part.is_junction())
                for part in (original, *original.parents)
            )
        ):
            raise unavailable()
        with path.open("rb") as source:
            data = source.read(cap + 1)
        if len(data) != snapshot.size or sha256(data).hexdigest() != snapshot.digest:
            raise unavailable()
        # Private downloads use attachment plus nosniff; bytes never choose an
        # executable response type. Dedicated image presentation validates MIME.
        return data, "application/octet-stream"
    except (OSError, ValueError):
        raise unavailable() from None
