"""Browser file labels and paths cannot claim a server-owned source."""

import pytest

from app.core.exceptions import BusinessException
from app.modules.ai.service.delivery_file_parts import part_file_id


def test_only_canonical_same_tenant_local_images_claim_a_file():
    assert (
        part_file_id(
            {
                "type": "file",
                "mediaType": "image/png",
                "url": "/uploads/tenant-71/2026/11/14/801.png",
            },
            71,
        )
        == 801
    )
    for url in (
        "/uploads/tenant-72/2026/11/14/801.png",
        "/uploads/tenant-71/2026/11/14/../801.png",
        "/uploads/tenant-71/2026/11/14/801.png?name=x",
    ):
        with pytest.raises(BusinessException):
            part_file_id({"type": "file", "mediaType": "image/png", "url": url}, 71)


def test_nonimage_file_reference_requires_a_positive_server_id():
    assert (
        part_file_id({"type": "file", "fileId": "801", "filename": "forged"}, 71) == 801
    )
    for value in (None, True, "../801", "01", 0):
        with pytest.raises(BusinessException):
            part_file_id({"type": "file", "fileId": value}, 71)
