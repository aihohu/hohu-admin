"""File parser metadata and worker inputs; no database handle crosses into IO."""

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.core.authorization_plan import ResourceKey, ResourceKind
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.tools.file_access import _not_found, read_protected_file
from app.modules.ai.agents.tools.file_parser import PARSERS
from app.modules.ai.agents.tools.file_tools import (
    _FILE_PARSE_ACCESS_POLICY,
    parsed_file_result,
)
from app.modules.system.models.file import File
from app.modules.system.service.file_policy_service import (
    UploadPolicy,
    file_policy_service,
)


@dataclass(frozen=True, slots=True)
class FileParseInput:
    facts: tuple
    policy: UploadPolicy


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
    parser = PARSERS.get(protected.mime_type)
    if parser is None:
        raise _order_error()
    parsed = parser._check_and_parse_bytes(protected.data)
    return parsed_file_result(record.file_id, parsed)
