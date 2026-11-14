"""Bounded upload format checks, before publishing any bytes to disk.

Container signatures identify a format family, not executable safety. Downloads
remain attachments; business parsers retain their own stricter format budgets.
"""

import codecs
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from app.core.exceptions import BusinessRuleException

MIMES = {
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".zip": "application/zip",
    ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}
ALIASES = {
    ".csv": {"text/plain", "application/vnd.ms-excel"},
    ".md": {"text/plain"},
    ".xml": {"text/xml"},
    ".zip": {"application/x-zip-compressed"},
    ".rar": {"application/x-rar-compressed"},
    ".gz": {"application/x-gzip"},
    ".wav": {"audio/x-wav"},
    ".ico": {"image/vnd.microsoft.icon"},
}


def _text(data):
    if b"\x00" in data:
        return False
    # Decode incrementally so a sample ending inside a multibyte character is
    # not mistaken for binary. No entity expansion or business parsing here.

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = codecs.getincrementaldecoder(encoding)(errors="strict").decode(
                data[:8192], final=len(data) <= 8192
            )
            return all(c >= " " or c in "\r\n\t" for c in text)
        except UnicodeError:
            continue
    return False


def _zip(ext, data):
    if not data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return False
    # Bound the declared directory before ZipFile allocates entry objects.
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        return False
    count = int.from_bytes(data[end + 10 : end + 12], "little")
    if (
        count > 4096
        or int.from_bytes(data[end + 12 : end + 16], "little") > 4 * 1024 * 1024
    ):
        return False
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) > 4096 or len(names) != len(set(names)):
                return False
            required = {
                ".docx": "word/document.xml",
                ".xlsx": "xl/workbook.xml",
                ".pptx": "ppt/presentation.xml",
            }.get(ext)
            return required is None or (
                required in names and "[Content_Types].xml" in names
            )
    except (BadZipFile, ValueError, OSError):
        return False


def validate_upload_content(ext, declared_mime, data):
    mime = (declared_mime or "").split(";", 1)[0].strip().lower()
    canonical = MIMES.get(ext)
    valid = canonical is not None and mime in {
        "",
        "application/octet-stream",
        canonical,
        *ALIASES.get(ext, ()),
    }
    if valid:
        if ext in {".txt", ".md", ".csv", ".json", ".xml"}:
            valid = _text(data)
        elif ext in {".zip", ".docx", ".xlsx", ".pptx"}:
            valid = _zip(ext, data)
        elif ext in {".doc", ".xls", ".ppt"}:
            valid = data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        elif ext == ".webp":
            valid = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        elif ext == ".wav":
            valid = data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        elif ext in {".mp4", ".mov"}:
            valid = data[4:8] == b"ftyp" and len(data) >= 12
        elif ext == ".tar":
            valid = len(data) >= 512 and data[257:262] == b"ustar"
        elif ext == ".mp3":
            valid = (
                data.startswith(b"ID3")
                or len(data) > 2
                and data[0] == 255
                and data[1] & 224 == 224
            )
        else:
            signatures = {
                ".gif": (b"GIF87a", b"GIF89a"),
                ".bmp": (b"BM",),
                ".ico": (b"\x00\x00\x01\x00",),
                ".pdf": (b"%PDF-",),
                ".rar": (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"),
                ".7z": (b"7z\xbc\xaf\x27\x1c",),
                ".gz": (b"\x1f\x8b",),
            }
            valid = data.startswith(signatures.get(ext, ()))
    if not valid:
        raise BusinessRuleException(
            "文件内容与格式不一致", error_code="FILE_CONTENT_INVALID"
        )
    return canonical
