"""Pure AST gate for explicit candidate coverage, without runtime imports or DB."""

from dataclasses import replace

from app.core.authorization_candidates import CANDIDATES
from tools.checks.check_authorization_candidates import check_candidates


def test_candidate_inventory_is_complete_and_dormant():
    assert check_candidates() == []
    assert all(not entry.production_connected for entry in CANDIDATES)


def test_unknown_io_profile_and_operation_are_rejected():
    first = CANDIDATES[0]
    assert any(
        "io_profile" in error
        for error in check_candidates(
            entries=(replace(first, io_profile="unbounded"), *CANDIDATES[1:])
        )
    )
    assert any(
        "operation" in error
        for error in check_candidates(
            entries=(replace(first, operation="user.unknown"), *CANDIDATES[1:])
        )
    )


def test_missing_method_duplicate_and_connected_claim_are_rejected():
    first = CANDIDATES[0]
    for changed, expected in (
        (
            replace(
                first,
                qualified_name="app.modules.auth.candidate_owner.AuthCandidateOwner.missing",
            ),
            "qualified_name",
        ),
        (replace(first, production_connected=True), "production_connected"),
        (replace(first, entry_id=CANDIDATES[1].entry_id), "duplicate"),
    ):
        assert any(
            expected in error
            for error in check_candidates(entries=(changed, *CANDIDATES[1:]))
        )


def test_new_public_async_candidate_cannot_escape_inventory():
    module = "app.modules.auth.candidate_owner"
    source = (
        "class AuthCandidateOwner:\n    async def surprise_write(self):\n        pass\n"
    )
    assert any(
        "unregistered" in error and "surprise_write" in error
        for error in check_candidates(module_sources={module: source})
    )


def test_removing_one_system_operation_fails_even_with_same_dispatch_method():
    assert any(
        "operation" in error for error in check_candidates(entries=CANDIDATES[1:])
    )
