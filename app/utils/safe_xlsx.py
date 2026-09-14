"""Fail-closed XML preflight for untrusted OOXML workbooks."""

import io
import zipfile
from pathlib import PurePosixPath
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as defused_fromstring

MAX_ZIP_ENTRIES = 2048
MAX_ENTRY_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0


class UnsafeXlsxError(ValueError):
    """The workbook archive or one of its XML parts is unsafe or malformed."""


def parse_untrusted_xml(data: bytes) -> Element:
    """Parse XML with DTD, entity and external-reference processing disabled."""
    try:
        return defused_fromstring(
            data,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, ParseError, TypeError, ValueError) as exc:
        raise UnsafeXlsxError("unsafe or malformed OOXML part") from exc


def validate_untrusted_xlsx_xml(data: bytes) -> None:
    """Bound ZIP expansion and safely parse every XML/relationship part.

    Callers retain ownership of workbook-specific structural checks and map
    ``UnsafeXlsxError`` to their stable domain error code.
    """
    if not data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise UnsafeXlsxError("not a ZIP archive")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise UnsafeXlsxError("too many ZIP entries")

            names: set[str] = set()
            total_uncompressed = 0
            total_compressed = 0
            xml_parts: list[zipfile.ZipInfo] = []
            for info in infos:
                path = PurePosixPath(info.filename)
                is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
                if (
                    not info.filename
                    or "\x00" in info.filename
                    or "\\" in info.filename
                    or path.is_absolute()
                    or ".." in path.parts
                    or (path.parts and ":" in path.parts[0])
                    or info.filename in names
                    or info.flag_bits & 0x1
                    or is_symlink
                ):
                    raise UnsafeXlsxError("unsafe ZIP entry")
                names.add(info.filename)

                if info.file_size > MAX_ENTRY_UNCOMPRESSED_BYTES:
                    raise UnsafeXlsxError("ZIP entry exceeds expansion budget")
                if (
                    info.file_size > 0
                    and info.file_size / max(1, info.compress_size)
                    > MAX_COMPRESSION_RATIO
                ):
                    raise UnsafeXlsxError("ZIP entry exceeds compression budget")
                total_uncompressed += info.file_size
                total_compressed += info.compress_size

                lowered = info.filename.lower()
                if lowered.endswith((".xml", ".rels")):
                    xml_parts.append(info)

            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES or (
                total_uncompressed > 0
                and total_uncompressed / max(1, total_compressed)
                > MAX_COMPRESSION_RATIO
            ):
                raise UnsafeXlsxError("archive exceeds expansion budget")

            for info in xml_parts:
                parse_untrusted_xml(archive.read(info))

            if archive.testzip() is not None:
                raise UnsafeXlsxError("corrupt ZIP entry")
    except UnsafeXlsxError:
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
    ) as exc:
        raise UnsafeXlsxError("invalid XLSX archive") from exc


__all__ = [
    "UnsafeXlsxError",
    "parse_untrusted_xml",
    "validate_untrusted_xlsx_xml",
]
