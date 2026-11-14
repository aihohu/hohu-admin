"""Preview MACs bind the complete bounded context, including large manifests."""

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.field_policy import management_preview as preview


def test_preview_supports_manifest_budget_and_rejects_changed_or_expired_context(
    monkeypatch,
):
    monkeypatch.setattr(preview.time, "time", lambda: 1000)
    context = {"items": ["a" * 100 for _ in range(16384)]}
    token, expires = preview.issue_preview(context)
    assert expires == 1300
    preview.verify_preview(token, context)
    for changed in ({"items": []}, {"actor": 2, **context}):
        with pytest.raises(BusinessException):
            preview.verify_preview(token, changed)
    monkeypatch.setattr(preview.time, "time", lambda: 1300)
    with pytest.raises(BusinessException):
        preview.verify_preview(token, context)


@pytest.mark.parametrize("token", ["invalid", "1.2.3", "١٢٣.x.y", "9999999999999.x.y"])
def test_forged_preview_fails_closed(token):
    with pytest.raises(BusinessException) as rejected:
        preview.verify_preview(token, {})
    assert rejected.value.error_code == "FIELD_POLICY_PREVIEW_STALE"
