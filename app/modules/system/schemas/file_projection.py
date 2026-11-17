"""Closed sparse file metadata; storage references are never model fields."""

from datetime import datetime

from pydantic import StrictStr

from app.modules.system.schemas.relation_policy import RelationFieldPolicy, RelationPage
from app.modules.system.schemas.relation_projection import SparseRelationOut


class FileFieldPolicy(RelationFieldPolicy):
    can_download: bool


class ProjectedFileOut(SparseRelationOut):
    file_id: StrictStr
    original_name: str | None = None
    file_size: int | None = None
    file_ext: str | None = None
    mime_type: str | None = None
    business_type: str | None = None
    create_time: datetime | None = None
    owner_name: str | None = None
    field_policy: FileFieldPolicy


class FilePage(RelationPage[ProjectedFileOut]):
    field_policy: FileFieldPolicy


class UploadReceipt(SparseRelationOut):
    file_id: StrictStr
    file_url: str
    original_name: str | None = None
    file_size: int | None = None
    file_ext: str | None = None
    mime_type: str | None = None
    business_type: str | None = None
