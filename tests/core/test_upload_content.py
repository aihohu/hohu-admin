import pytest

from app.core.exceptions import BusinessException
from app.core.upload_content import validate_upload_content


@pytest.mark.parametrize(
    "ext,mime,data",
    [
        (".pdf", "application/pdf", b"not a pdf"),
        (
            ".xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b"PK fake",
        ),
        (".txt", "text/plain", b"MZ\x00\x01executable"),
        (".txt", "application/pdf", b"plain text"),
        (".zip", "application/zip", b"not a zip"),
    ],
)
def test_extension_or_declared_mime_cannot_disguise_binary_content(ext, mime, data):
    with pytest.raises(BusinessException) as caught:
        validate_upload_content(ext, mime, data)
    assert caught.value.error_code == "FILE_CONTENT_INVALID"


@pytest.mark.parametrize(
    "mime", [None, "", "application/octet-stream", "text/plain; charset=utf-8"]
)
def test_plain_text_has_a_canonical_mime(mime):
    assert validate_upload_content(".txt", mime, b"hello\n") == "text/plain"
