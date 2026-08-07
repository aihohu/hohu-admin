"""Protected ``sys_file`` access for AI tools.

``file_id`` is a resource reference, not an authorization grant.  This module
keeps the resource ACL, content checks, size bound, and storage-root boundary in
one place so business tools cannot regress to ``select(File) + read_bytes``.
"""

import io
import struct
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.ai.core.context import AiToolContext
from app.modules.system.models.file import File

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
APPLICATION_CSV_MIME = "application/csv"
TEXT_MIME = "text/plain"

XLSX_MAX_ZIP_ENTRIES = 2048
XLSX_MAX_ENTRY_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
XLSX_MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
XLSX_MAX_COMPRESSION_RATIO = 200.0

_XLSX_REQUIRED_MEMBERS = frozenset(
    {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
    }
)
_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_RELATIONSHIPS_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_SPREADSHEET_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)

IMPORT_MIME_TYPES_BY_EXTENSION: Mapping[str, frozenset[str]] = {
    ".xlsx": frozenset({XLSX_MIME}),
    ".csv": frozenset({CSV_MIME}),
}

AI_CHAT_MIME_TYPES_BY_EXTENSION: Mapping[str, frozenset[str]] = {
    ".xlsx": frozenset({XLSX_MIME}),
    ".csv": frozenset({CSV_MIME, APPLICATION_CSV_MIME, TEXT_MIME}),
    ".txt": frozenset({TEXT_MIME}),
}


def private_upload_root(_record: File) -> Path:
    """Resolve the non-public upload root at call time."""
    return Path(settings.PRIVATE_UPLOAD_DIR)


def chat_or_private_upload_root(record: File) -> Path:
    """Use the root that corresponds to the server-assigned business type."""
    if record.business_type in {"ai-chat-private", "user-import"}:
        return private_upload_root(record)
    return Path(settings.UPLOAD_DIR)


@dataclass(frozen=True)
class FileAccessPolicy:
    """Allowlist and storage boundary for one AI file-consuming tool."""

    allowed_business_types: frozenset[str]
    mime_types_by_extension: Mapping[str, frozenset[str]]
    max_bytes: int
    storage_root_resolver: Callable[[File], Path] = private_upload_root


@dataclass(frozen=True)
class ProtectedFile:
    """A file that passed resource and content validation."""

    record: File
    path: Path
    data: bytes
    mime_type: str


def _not_found() -> BusinessRuleException:
    # The same response is used for missing/deleted/legacy/cross-scope records to
    # avoid leaking whether a guessed Snowflake ID exists.
    return BusinessRuleException(
        "文件不存在或不可访问",
        error_code="AI_FILE_NOT_FOUND",
    )


def _type_not_allowed() -> BusinessRuleException:
    return BusinessRuleException(
        "文件类型不允许",
        error_code="AI_FILE_TYPE_NOT_ALLOWED",
    )


def _too_large(max_bytes: int) -> BusinessRuleException:
    return BusinessRuleException(
        f"文件超过 {max_bytes // (1024 * 1024)}MB 限制",
        error_code="AI_FILE_TOO_LARGE",
    )


def _path_invalid() -> BusinessRuleException:
    return BusinessRuleException(
        "文件存储路径无效",
        error_code="AI_FILE_PATH_INVALID",
    )


def _normalize_mime_type(value: str | None) -> str:
    """Drop optional charset parameters and compare MIME values case-insensitively."""
    return (value or "").split(";", maxsplit=1)[0].strip().lower()


def _resolve_path_within_root(file_path: str, root: Path) -> Path:
    resolved_root = root.resolve()
    raw_path = Path(file_path)
    candidate = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    resolved_path = candidate.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise _path_invalid()
    return resolved_path


def _looks_like_text(data: bytes) -> bool:
    """Conservative CSV/TXT magic check without trusting the declared MIME."""
    # Empty text is structurally valid; the business parser owns the more useful
    # AI_IMPORT_EMPTY_FILE / empty-preview semantic.
    if not data:
        return True
    if b"\x00" in data:
        return False
    sample = data[:8192]
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = sample.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        controls = sum(ch < " " and ch not in "\t\r\n" for ch in text)
        return controls <= max(1, len(text) // 100)
    return False


def _magic_matches(extension: str, data: bytes) -> bool:
    if extension in {".csv", ".txt"}:
        return _looks_like_text(data)
    return False


def _safe_zip_member_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not (path.parts and ":" in path.parts[0])
    )


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _xml_root_matches(data: bytes, namespace: str, local_name: str) -> bool:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return False
    return root.tag == f"{{{namespace}}}{local_name}"


def _content_types_declares_xlsx(data: bytes) -> bool:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return False
    if root.tag != f"{{{_CONTENT_TYPES_NAMESPACE}}}Types":
        return False
    override_tag = f"{{{_CONTENT_TYPES_NAMESPACE}}}Override"
    return any(
        child.tag == override_tag
        and child.attrib.get("PartName") == "/xl/workbook.xml"
        and child.attrib.get("ContentType") == _WORKBOOK_CONTENT_TYPE
        for child in root
    )


def _declared_zip_entry_count(data: bytes, max_bytes: int) -> int:
    """Read EOCD before ZipFile can materialize an attacker-sized file list."""
    signature = b"PK\x05\x06"
    search_start = max(0, len(data) - (65_535 + 22))
    search_end = len(data)
    while True:
        offset = data.rfind(signature, search_start, search_end)
        if offset < 0:
            raise _type_not_allowed()
        if offset + 22 <= len(data):
            (
                disk_number,
                central_directory_disk,
                entries_on_disk,
                total_entries,
                central_directory_size,
                central_directory_offset,
                comment_length,
            ) = struct.unpack_from("<4H2LH", data, offset + 4)
            if offset + 22 + comment_length == len(data):
                if (
                    disk_number != 0
                    or central_directory_disk != 0
                    or entries_on_disk != total_entries
                ):
                    raise _type_not_allowed()
                if (
                    total_entries == 0xFFFF
                    or central_directory_size == 0xFFFFFFFF
                    or central_directory_offset == 0xFFFFFFFF
                ):
                    raise _too_large(max_bytes)
                if total_entries > XLSX_MAX_ZIP_ENTRIES:
                    raise _too_large(max_bytes)
                if central_directory_offset + central_directory_size != offset:
                    raise _type_not_allowed()
                return total_entries
        search_end = offset


def validate_xlsx_archive(data: bytes, max_bytes: int) -> None:
    """Validate OOXML structure and expansion budgets before any parser runs."""
    if not data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise _type_not_allowed()

    declared_entries = _declared_zip_entry_count(data, max_bytes)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) != declared_entries:
                raise _type_not_allowed()
            if len(infos) > XLSX_MAX_ZIP_ENTRIES:
                raise _too_large(max_bytes)

            names: set[str] = set()
            total_uncompressed = 0
            total_compressed = 0
            for info in infos:
                if (
                    not _safe_zip_member_name(info.filename)
                    or info.filename in names
                    or info.flag_bits & 0x1
                    or _is_symlink(info)
                ):
                    raise _type_not_allowed()
                names.add(info.filename)

                if info.file_size > XLSX_MAX_ENTRY_UNCOMPRESSED_BYTES:
                    raise _too_large(max_bytes)
                if (
                    info.file_size > 0
                    and info.file_size / max(1, info.compress_size)
                    > XLSX_MAX_COMPRESSION_RATIO
                ):
                    raise _too_large(max_bytes)
                total_uncompressed += info.file_size
                total_compressed += info.compress_size

            if total_uncompressed > XLSX_MAX_TOTAL_UNCOMPRESSED_BYTES or (
                total_uncompressed > 0
                and total_uncompressed / max(1, total_compressed)
                > XLSX_MAX_COMPRESSION_RATIO
            ):
                raise _too_large(max_bytes)

            if not _XLSX_REQUIRED_MEMBERS.issubset(names) or not any(
                name.startswith("xl/worksheets/") and name.endswith(".xml")
                for name in names
            ):
                raise _type_not_allowed()

            # CRC-check every member only after metadata budgets make expansion bounded.
            if archive.testzip() is not None:
                raise _type_not_allowed()
            if not _content_types_declares_xlsx(archive.read("[Content_Types].xml")):
                raise _type_not_allowed()
            if not _xml_root_matches(
                archive.read("_rels/.rels"),
                _RELATIONSHIPS_NAMESPACE,
                "Relationships",
            ):
                raise _type_not_allowed()
            if not _xml_root_matches(
                archive.read("xl/workbook.xml"),
                _SPREADSHEET_NAMESPACE,
                "workbook",
            ):
                raise _type_not_allowed()
            if not _xml_root_matches(
                archive.read("xl/_rels/workbook.xml.rels"),
                _RELATIONSHIPS_NAMESPACE,
                "Relationships",
            ):
                raise _type_not_allowed()
    except BusinessRuleException:
        raise
    except (
        EOFError,
        KeyError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        raise _type_not_allowed() from None


async def load_protected_file(
    ctx: AiToolContext,
    file_id: str,
    *,
    policy: FileAccessPolicy,
) -> ProtectedFile:
    """Load one AI-visible file after validating ACL and content boundaries."""
    try:
        file_id_int = int(file_id)
    except (TypeError, ValueError) as exc:
        raise BusinessRuleException(
            f"file_id 格式无效: {file_id!r}",
            error_code="AI_FILE_ID_INVALID",
        ) from exc

    result = await ctx.db.execute(select(File).where(File.file_id == file_id_int))
    record = result.scalars().first()

    trusted_tenant_id = getattr(ctx, "tenant_id", None)
    if (
        record is None
        or record.del_flag != "0"
        or getattr(record, "owner_user_id", None) != ctx.user.user_id
        or trusted_tenant_id is None
        or getattr(record, "tenant_id", None) != trusted_tenant_id
    ):
        raise _not_found()

    if record.business_type not in policy.allowed_business_types:
        raise _type_not_allowed()

    extension = (record.file_ext or "").strip().lower()
    mime_type = _normalize_mime_type(record.mime_type)
    allowed_mime_types = policy.mime_types_by_extension.get(extension)
    if allowed_mime_types is None or mime_type not in allowed_mime_types:
        raise _type_not_allowed()

    # Deployment settings may lower the tool limit but can never raise it.
    max_bytes = min(policy.max_bytes, settings.UPLOAD_MAX_SIZE)
    if record.file_size < 0 or record.file_size > max_bytes:
        raise _too_large(max_bytes)

    resolved_path = _resolve_path_within_root(
        record.file_path,
        policy.storage_root_resolver(record),
    )
    if resolved_path.suffix.lower() != extension:
        raise _type_not_allowed()

    try:
        stat = resolved_path.stat()  # noqa: ASYNC240
    except (FileNotFoundError, NotADirectoryError):
        raise _not_found() from None
    except OSError:
        raise _path_invalid() from None
    if not resolved_path.is_file():  # noqa: ASYNC240
        raise _not_found()
    if stat.st_size > max_bytes:
        raise _too_large(max_bytes)

    try:
        # Bounded read closes the stat/read TOCTOU window without allocating an
        # attacker-controlled file larger than the configured cap.
        with resolved_path.open("rb") as stream:  # noqa: ASYNC240
            data = stream.read(max_bytes + 1)
    except (FileNotFoundError, NotADirectoryError):
        raise _not_found() from None
    except OSError:
        raise _path_invalid() from None

    if len(data) > max_bytes:
        raise _too_large(max_bytes)
    if extension == ".xlsx":
        validate_xlsx_archive(data, max_bytes)
    elif not _magic_matches(extension, data):
        raise _type_not_allowed()

    return ProtectedFile(
        record=record,
        path=resolved_path,
        data=data,
        mime_type=mime_type,
    )
