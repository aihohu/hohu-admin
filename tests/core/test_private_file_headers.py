"""Private download bytes must not be replayed from a shared browser cache."""

import pytest
from starlette.responses import Response

from app.core.file_delivery import private_file_headers


@pytest.mark.parametrize(
    "filename",
    [
        "users.xlsx",
        "errors.xlsx",
        "资料.csv",
        'quote";x=.txt',
        "line\r\nInjected: yes.txt",
        "../../secret.txt",
    ],
)
def test_headers_are_private_and_filename_is_a_single_safe_attachment(filename):
    headers = private_file_headers(filename)
    response = Response(b"body", headers=headers)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    value = response.headers["content-disposition"]
    assert value.startswith("attachment;")
    assert "\r" not in value and "\n" not in value
    assert "../" not in value
