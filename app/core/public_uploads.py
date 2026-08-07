"""Static-file boundary for genuinely public uploads."""

from pathlib import PurePosixPath

from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

_PRIVATE_DOCUMENT_EXTENSIONS = frozenset({".csv", ".txt", ".xls", ".xlsx"})


def _normalize_windows_component(value: str) -> str:
    return value.split(":", maxsplit=1)[0].rstrip(" .").casefold()


class PublicUploadStaticFiles(StaticFiles):
    """Serve public uploads while quarantining the legacy artifact namespace.

    Older releases wrote import/export artifacts under ``uploads/file_storage``.
    They remain readable through authenticated ``FileStorage`` fallback during
    upgrade, but GET/HEAD through the public mount must always look absent.
    """

    async def get_response(self, path: str, scope: dict) -> Response:
        parts = tuple(
            _normalize_windows_component(part)
            for part in PurePosixPath(path.replace("\\", "/")).parts
            if part not in {"", "."}
        )
        leaf = PurePosixPath(path.replace("\\", "/")).name
        # Win32 may resolve trailing dots/spaces or an NTFS ADS suffix to the
        # same underlying file.  Normalize those aliases before the deny check.
        normalized_leaf = _normalize_windows_component(leaf)
        suffix = PurePosixPath(normalized_leaf).suffix
        if (parts and parts[0] == "file_storage") or (
            suffix in _PRIVATE_DOCUMENT_EXTENSIONS
        ):
            return Response(status_code=404)
        return await super().get_response(path, scope)
