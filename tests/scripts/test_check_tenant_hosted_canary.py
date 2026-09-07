from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.tenant_canary import build_canary_preflight_report
from scripts import check_tenant_hosted_canary


def _mapping_result(value):
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = value
    return result


async def test_pre_activation_report_accepts_one_bootstrapped_prepared_target() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_mapping_result(
            {
                "tenant_id": 22,
                "status": "2",
                "lifecycle_state": "prepared",
                "bootstrap_version": 1,
                "row_version": 3,
            }
        )
    )
    db.scalar = AsyncMock(return_value=0)

    report = await build_canary_preflight_report(
        db,
        build_sha="a" * 40,
        phase="pre_activation",
        target_tenant_id=22,
    )

    assert report.risk_count == 0
    assert report.as_dict()["targetTenantId"] == "22"
    assert report.as_dict()["database"]["nonTargetActiveCount"] == 0


async def test_post_activation_report_fails_for_wrong_state_and_other_active_tenant() -> (
    None
):
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_mapping_result(
            {
                "tenant_id": 22,
                "status": "2",
                "lifecycle_state": "prepared",
                "bootstrap_version": 1,
                "row_version": 3,
            }
        )
    )
    db.scalar = AsyncMock(return_value=1)

    report = await build_canary_preflight_report(
        db,
        build_sha="b" * 40,
        phase="post_activation",
        target_tenant_id=22,
    )

    assert report.risk_count == 2
    assert {risk["code"] for risk in report.as_dict()["risks"]} == {
        "CANARY_TARGET_NOT_ACTIVE",
        "NON_TARGET_TENANT_ACTIVE",
    }


async def test_preflight_report_fails_closed_when_target_is_missing() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_mapping_result(None))
    db.scalar = AsyncMock(return_value=0)

    report = await build_canary_preflight_report(
        db,
        build_sha="c" * 40,
        phase="pre_activation",
        target_tenant_id=22,
    )

    assert report.risk_count == 1
    assert report.as_dict()["risks"] == [
        {"code": "CANARY_TARGET_NOT_FOUND", "count": 1}
    ]


def test_verified_build_sha_requires_clean_matching_checkout(monkeypatch) -> None:
    calls = iter(
        [
            MagicMock(stdout=f"{'a' * 40}\n"),
            MagicMock(stdout=""),
        ]
    )
    monkeypatch.setattr(
        check_tenant_hosted_canary.subprocess,
        "run",
        lambda *_args, **_kwargs: next(calls),
    )
    monkeypatch.setattr(
        check_tenant_hosted_canary.settings,
        "RELEASE_BUILD_SHA",
        "a" * 40,
    )

    assert check_tenant_hosted_canary._verified_build_sha("a" * 40) == "a" * 40


@pytest.mark.parametrize(
    ("revision", "worktree", "expected"),
    [
        ("a" * 40, " M app/core/config.py", "clean Git checkout"),
        ("a" * 40, "", "does not match"),
    ],
)
def test_verified_build_sha_rejects_dirty_or_mismatched_checkout(
    monkeypatch, revision: str, worktree: str, expected: str
) -> None:
    calls = iter([MagicMock(stdout=revision), MagicMock(stdout=worktree)])
    monkeypatch.setattr(
        check_tenant_hosted_canary.subprocess,
        "run",
        lambda *_args, **_kwargs: next(calls),
    )
    monkeypatch.setattr(
        check_tenant_hosted_canary.settings,
        "RELEASE_BUILD_SHA",
        "",
    )

    with pytest.raises(ValueError, match=expected):
        check_tenant_hosted_canary._verified_build_sha("b" * 40)


def test_verified_build_sha_rejects_an_abbreviated_revision() -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        check_tenant_hosted_canary._verified_build_sha("a" * 12)
