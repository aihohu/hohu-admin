"""Public upload mount must never expose private/legacy AI artifacts."""

import pytest

from app.core.public_uploads import PublicUploadStaticFiles


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_legacy_file_storage_namespace_is_not_served(tmp_path, method) -> None:
    secret = tmp_path / "file_storage" / "import-preview" / "secret.xlsx"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"secret")
    static = PublicUploadStaticFiles(directory=tmp_path)

    response = await static.get_response(
        "file_storage/import-preview/secret.xlsx",
        {"type": "http", "method": method, "headers": []},
    )

    assert response.status_code == 404


async def test_normal_public_upload_is_still_served(tmp_path) -> None:
    public_file = tmp_path / "2026" / "08" / "avatar.png"
    public_file.parent.mkdir(parents=True)
    public_file.write_bytes(b"png")
    static = PublicUploadStaticFiles(directory=tmp_path)

    response = await static.get_response(
        "2026/08/avatar.png",
        {"type": "http", "method": "GET", "headers": []},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("suffix", [".csv", ".txt", ".xls", ".xlsx"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_historical_chat_documents_are_not_served_anonymously(
    tmp_path, suffix, method
) -> None:
    private_document = tmp_path / "2026" / "08" / f"snowflake{suffix}"
    private_document.parent.mkdir(parents=True)
    private_document.write_bytes(b"sensitive")
    static = PublicUploadStaticFiles(directory=tmp_path)

    response = await static.get_response(
        f"2026/08/snowflake{suffix}",
        {"type": "http", "method": method, "headers": []},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "requested_name",
    ["snowflake.xlsx.", "snowflake.xlsx ", "snowflake.xlsx::$DATA"],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_windows_filename_aliases_cannot_bypass_document_deny(
    tmp_path, requested_name, method
) -> None:
    private_document = tmp_path / "snowflake.xlsx"
    private_document.write_bytes(b"sensitive")
    static = PublicUploadStaticFiles(directory=tmp_path)

    response = await static.get_response(
        requested_name,
        {"type": "http", "method": method, "headers": []},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "namespace_alias",
    ["file_storage.", "file_storage ", "file_storage::$DATA"],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_windows_aliases_cannot_bypass_legacy_namespace_deny(
    tmp_path, namespace_alias, method
) -> None:
    legacy_file = tmp_path / "file_storage" / "secret.bin"
    legacy_file.parent.mkdir()
    legacy_file.write_bytes(b"secret")
    static = PublicUploadStaticFiles(directory=tmp_path)

    response = await static.get_response(
        f"{namespace_alias}/secret.bin",
        {"type": "http", "method": method, "headers": []},
    )

    assert response.status_code == 404
