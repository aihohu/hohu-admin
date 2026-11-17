"""Private binary response metadata, independent from authorization decisions."""

import re
from urllib.parse import quote


def private_file_headers(filename):
    # URLs/storage paths are never accepted as download filenames. Keep the
    # ASCII case stable for older clients; quote arbitrary Unicode in RFC 5987.
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    name = (
        "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:255]
        or "download.bin"
    )
    disposition = (
        "attachment; filename=" + name
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name)
        else "attachment; filename=download.bin; filename*=UTF-8''"
        + quote(name, safe="")
    )
    return {
        "Content-Disposition": disposition,
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
