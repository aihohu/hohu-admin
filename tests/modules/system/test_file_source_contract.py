"""File provenance is a closed fact, never an uploader-supplied capability."""

from dataclasses import replace
from hashlib import sha256

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.file_sources import (
    FileContentSnapshot,
    make_file_manifest,
    read_file_content,
    require_file_manifest,
    upload_delivery,
)


@pytest.mark.parametrize(
    "kind,ext,expected",
    [
        (None, ".png", "owned-private"),
        ("product", ".png", "owned-private"),
        ("brand", ".png", "public-image"),
        ("avatar", ".jpg", "public-image"),
        ("ai-chat-image", ".png", "ai-chat-image"),
        ("user-import", ".xlsx", "user-import"),
    ],
)
def test_only_explicit_fixed_image_categories_are_public(kind, ext, expected):
    assert upload_delivery(ext, kind, None) == expected


@pytest.mark.parametrize("kind", [None, "brand", "avatar", "product", "user-import"])
def test_client_business_id_does_not_create_a_source_binding(kind):
    with pytest.raises(BusinessException) as error:
        upload_delivery(".png", kind, 301)
    assert error.value.error_code == "FILE_BUSINESS_SOURCE_REQUIRED"


def snapshot(path, data=b"hello"):
    digest = sha256(data).hexdigest()
    proof = make_file_manifest(
        tenant_id=71,
        owner_id=101,
        file_id=801,
        delivery_class="owned-private",
        digest=digest,
    )
    return FileContentSnapshot(
        71,
        801,
        101,
        str(path),
        ".txt",
        "text/plain",
        len(data),
        "owned-private",
        digest,
        proof,
    )


def test_manifest_cannot_relabel_owner_tenant_or_file(tmp_path):
    value = snapshot(tmp_path / "file.txt")
    assert require_file_manifest(value) == value
    for changed in (
        replace(value, tenant_id=72),
        replace(value, owner_id=102),
        replace(value, file_id=802),
        replace(value, delivery_class="public-image"),
        replace(value, manifest={**value.manifest, "storage_key": "secret"}),
    ):
        with pytest.raises(BusinessException):
            require_file_manifest(changed)


def test_bounded_reader_verifies_hash_and_root_before_delivery(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    file = private / "source.txt"
    file.write_bytes(b"hello")
    value = snapshot(file)
    assert (
        read_file_content(value, str(tmp_path / "public"), str(private))[0] == b"hello"
    )
    file.write_bytes(b"other")
    with pytest.raises(BusinessException):
        read_file_content(value, str(tmp_path / "public"), str(private))
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"hello")
    with pytest.raises(BusinessException):
        read_file_content(
            replace(value, path=str(outside)), str(tmp_path / "public"), str(private)
        )


def test_unknown_historical_file_is_not_invented_provenance(tmp_path):
    value = replace(
        snapshot(tmp_path / "file.txt"),
        delivery_class="legacy-unknown",
        digest=None,
        manifest=None,
    )
    with pytest.raises(BusinessException):
        require_file_manifest(value)
