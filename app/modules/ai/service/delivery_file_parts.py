"""Canonical FILE references and current, server-derived attachment metadata."""

import re

from app.core.authorization_plan import ResourceKey, ResourceKind
from app.modules.ai.service.delivery_external_sources import file_source
from app.modules.ai.service.delivery_source_revalidation import changed


def part_file_id(part, tenant_id):
    if type(part) is not dict or part.get("type") != "file":
        return None
    url = part.get("url", "")
    if type(url) is not str:
        raise changed()
    if str(part.get("mediaType", "")).startswith("image/"):
        if url.startswith("/uploads/"):
            match = re.fullmatch(
                r"/uploads/tenant-(0|[1-9][0-9]*)/[0-9]{4}/[0-9]{2}/[0-9]{2}/([1-9][0-9]{0,18})\.(png|jpg|jpeg)",
                url,
            )
            if match is None or int(match[1]) != tenant_id or int(match[2]) >= 2**63:
                raise changed()
            return int(match[2])
        if url.startswith(("data:image/", "https://")):
            return None  # Original external user input, never a local FILE claim.
        raise changed()
    value = part.get("fileId", part.get("file_id"))
    if (
        type(value) not in {str, int}
        or re.fullmatch(r"[1-9][0-9]{0,18}", str(value)) is None
        or int(value) >= 2**63
    ):
        raise changed()
    return int(value)


def part_keys(parts, tenant):
    if parts is None:
        return ()
    if type(parts) is not list or len(parts) > 64:
        raise changed()
    return tuple(
        sorted(
            {
                ResourceKey(ResourceKind.FILE, tenant.tenant_id, identifier)
                for part in parts
                if (identifier := part_file_id(part, tenant.tenant_id)) is not None
            },
            key=lambda key: key.identity,
        )
    )


async def normalize_parts(db, tx, *, tenant, version, parts):
    if not parts:
        return parts, []
    output, groups = [], []
    for part in parts:
        if type(part) is not dict:
            raise changed()
        identifier = part_file_id(part, tenant.tenant_id)
        if identifier is None:
            if part.get("type") == "text":
                output.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "file":
                output.append(
                    {
                        "type": "file",
                        "url": part["url"],
                        "mediaType": part.get("mediaType", ""),
                    }
                )
            else:
                raise changed()
            continue
        tx.assert_held(ResourceKey(ResourceKind.FILE, tenant.tenant_id, identifier))
        group, row, decisions = await file_source(
            db,
            tx,
            tenant=tenant,
            version=version,
            identifier=identifier,
            action="file.chat-read",
        )
        value = {"type": "file", "fileId": str(identifier), "url": ""}
        reads = ["content"]
        for wire, name in (
            ("filename", "original_name"),
            ("fileSize", "file_size"),
            ("mediaType", "mime_type"),
        ):
            if decisions[name].allowed:
                value[wire] = getattr(row, name)
                reads.append(name)
        if str(row.mime_type or "").startswith("image/"):
            if not decisions["mime_type"].allowed:
                # The renderer/provider needs a truthful media type. A denied
                # cell cannot be reintroduced as a transport convenience.
                raise changed()
            canonical_url = row.file_url
            if (
                part_file_id(
                    {"type": "file", "mediaType": row.mime_type, "url": canonical_url},
                    tenant.tenant_id,
                )
                != identifier
            ):
                raise changed()
            value["url"] = canonical_url
        group["read"] = sorted(reads)
        groups.append(group)
        output.append(value)
    return output, groups
