"""File parser metadata and worker inputs; no database handle crosses into IO."""

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.core.authorization_plan import ResourceKey, ResourceKind
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult
from app.modules.ai.agents.tools.file_access import _not_found, read_protected_file
from app.modules.ai.agents.tools.file_parser import PARSERS
from app.modules.ai.agents.tools.file_tools import (
    _FILE_PARSE_ACCESS_POLICY,
    parsed_file_result,
)
from app.modules.system.field_policy.file_sources import require_file_manifest
from app.modules.system.models.file import File
from app.modules.system.service.file_policy_service import (
    UploadPolicy,
    file_policy_service,
)
from app.modules.system.service.file_read_service import content_snapshot


@dataclass(frozen=True, slots=True)
class FileParseInput:
    facts: tuple
    policy: UploadPolicy
    field_authority: dict | None = None


def file_parse_failure(snapshot, error):
    """Called only after the owner has revalidated the detached worker snapshot."""
    if type(snapshot) is not FileParseInput or not isinstance(error, BusinessException):
        raise _order_error()
    result = ToolResult.failure(error.error_code or "AI_INTERNAL_ERROR", error.message)
    result.projection = ResultProjection(
        subject_refs=({"type": "file", "id": str(dict(snapshot.facts)["file_id"])},),
        field_authority=snapshot.field_authority,
    )
    return result


def file_parse_key(tenant, args):
    value = args.get("file_id")
    try:
        identifier = int(value)
    except (TypeError, ValueError):
        raise _not_found() from None
    if isinstance(value, bool) or not 0 < identifier < 2**63:
        raise _not_found()
    return ResourceKey(ResourceKind.FILE, tenant.tenant_id, identifier)


async def file_parse_input(ctx, file_id, hint=""):
    if not isinstance(hint, str):
        raise _order_error()
    key = file_parse_key(ctx.tenant, {"file_id": file_id})
    record = (
        (
            await ctx.db.execute(
                select(File.__table__).where(
                    File.tenant_id == ctx.tenant.tenant_id,
                    File.file_id == key.identity[0],
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        record is None
        or record["del_flag"] != "0"
        or record["owner_user_id"] != ctx.tenant.actor_user_id
    ):
        raise _not_found()
    return FileParseInput(
        tuple(sorted(record.items())),
        await file_policy_service.resolve(ctx.db, "ai_file", tenant=ctx.tenant),
    )


def parse_uploaded_file(snapshot, public_root, private_root):
    if type(snapshot) is not FileParseInput:
        raise _order_error()
    record = SimpleNamespace(**dict(snapshot.facts))
    root = (
        private_root
        if record.business_type in {"ai-chat-private", "user-import"}
        else public_root
    )
    protected = read_protected_file(
        record,
        policy=replace(
            _FILE_PARSE_ACCESS_POLICY, storage_root_resolver=lambda _: Path(root)
        ),
        tenant_policy=snapshot.policy,
    )
    if getattr(record, "delivery_class", "legacy-unknown") != "legacy-unknown":
        require_file_manifest(content_snapshot(record))
        if sha256(protected.data).hexdigest() != record.content_sha256:
            raise _not_found()
    parser = PARSERS.get(protected.mime_type)
    if parser is None:
        raise _order_error()
    parsed = parser._check_and_parse_bytes(protected.data)
    result = parsed_file_result(record.file_id, parsed)
    proof = snapshot.field_authority
    if proof is not None:
        if not any(
            g.get("kind") == "file" and "file_size" in g["read"]
            for g in proof["source_domains"]
        ):
            result.data.pop("file_size", None)
        result.projection = replace(result.projection, field_authority=proof)
    return result
