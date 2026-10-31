"""Pure AST gate for explicit candidate coverage, without runtime imports or DB."""

from dataclasses import replace

from app.core.authorization_candidates import CANDIDATES
from tools.checks.check_authorization_candidates import check_candidates


def test_new_runtime_facade_requires_an_inventory_entry():
    module = "app.db.authorization_runtime"
    source = "class AuthorizationRuntime:\n    async def unchecked_write(self):\n        pass\n"
    assert any(
        "unregistered public async candidate: "
        + module
        + ".AuthorizationRuntime.unchecked_write"
        in error
        for error in check_candidates(module_sources={module: source})
    )


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


def test_control_operations_and_maintenance_fences_are_not_optional():
    for entry_id in (
        "catalog.provider.create",
        "configuration.mail.save",
        "tenant.tenant.prepare",
    ):
        assert any(
            "operation" in error
            for error in check_candidates(
                entries=tuple(
                    entry for entry in CANDIDATES if entry.entry_id != entry_id
                )
            )
        )
    changed = tuple(
        replace(entry, maintenance=False)
        if entry.entry_id == "principal.permissions"
        else entry
        for entry in CANDIDATES
    )
    assert any("maintenance" in error for error in check_candidates(entries=changed))


def test_new_platform_owner_and_external_lock_claim_fail():
    module = "app.modules.platform.authorization_principal_owner"
    assert any(
        "unregistered" in error
        for error in check_candidates(
            module_sources={module: "async def surprise(): pass"}
        )
    )
    changed = tuple(
        replace(entry, G_mode="S")
        if entry.entry_id == "configuration.run_mail"
        else entry
        for entry in CANDIDATES
    )
    assert any("external IO" in error for error in check_candidates(entries=changed))


def test_external_transport_and_parser_helpers_are_explicitly_registered():
    for entry_id in ("catalog.probe_transport", "configuration.import_parser"):
        entries = tuple(entry for entry in CANDIDATES if entry.entry_id != entry_id)
        assert any(
            "unregistered" in error for error in check_candidates(entries=entries)
        )


def test_global_guard_does_not_misdescribe_target_barrier_inventory():
    entries = {entry.entry_id: entry for entry in CANDIDATES}
    assert "new-B-only" in entries["tenant.tenant.prepare"].tenant_resolver
    assert "target-X" in entries["bootstrap.execute"].tenant_resolver
    assert "all-registered" not in entries["bootstrap.execute"].tenant_resolver
    assert entries["catalog.model_policy.put"].G_mode == "S"
    assert "all-registered" in entries["catalog.provider.update"].tenant_resolver
    assert "no-tenant-barrier" in entries["principal.current"].tenant_resolver
