"""Deterministic Plan 5-B-C tenant isolation audit contracts."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.tenant_isolation_audit import (
    IsolationAuditIssue,
    TenantIsolationAuditReport,
    scan_source_boundaries,
)
from tools.ops import audit_tenant_isolation as cli

_CANDIDATE_NAMESPACES = (
    (
        "modules/system/service/authorization_chat_image_candidate.py",
        "tenant_id",
        "cache",
    ),
    ("modules/system/service/authorization_upload_candidate.py", "tenant_id", "cache"),
    ("db/authorization_runtime.py", "TransferOwner", "cache"),
    ("db/authorization_lifecycle_owner.py", "tenant_id", "cache"),
    ("db/authorization_preview_owner.py", "tenant_id", "cache"),
    ("db/authorization_transfer_cache.py", "tenant_id", "storage"),
    ("db/authorization_transfer_owner.py", "tenant_id", "storage"),
    ("db/authorization_transfer_http.py", "tenant_id", "cache"),
    ("modules/ai/agents/gateway/authorization_execution.py", "tenant_id", "cache"),
    ("modules/ai/service/authorization_execution_candidate.py", "tenant_id", "cache"),
    ("modules/ai/service/authorization_lifecycle_candidate.py", "tenant_id", "cache"),
    ("modules/system/service/authorization_ai_file_adapter.py", "tenant_id", "cache"),
    (
        "modules/system/service/authorization_transfer_candidate.py",
        "tenant_id",
        "cache",
    ),
    ("db/authorization_control_owner.py", "cache_keys", "storage"),
    ("db/authorization_system_owner.py", "tenant_cache_key", "storage"),
    (
        "modules/system/service/authorization_import_candidate.py",
        "tenant_id",
        "cache",
    ),
)


def test_complete_source_namespace_inventory_is_clean():
    assert scan_source_boundaries().issues == ()


def _copy_candidate_namespace(tmp_path, source_path: str) -> tuple[Path, Path]:
    app_root = tmp_path / "app"
    target = app_root / source_path
    target.parent.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "app" / source_path
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return app_root, target


@pytest.mark.parametrize("namespace_case", _CANDIDATE_NAMESPACES)
def test_candidate_namespace_real_sources_are_precisely_registered(
    tmp_path, namespace_case
) -> None:
    source_path, _identifier, _extra_kind = namespace_case
    app_root, _ = _copy_candidate_namespace(tmp_path, source_path)

    result = scan_source_boundaries(app_root)

    assert result.issues == ()
    assert result.namespace_checks["registeredCount"] == 1
    assert result.namespace_checks["validCount"] == 1


@pytest.mark.parametrize("namespace_case", _CANDIDATE_NAMESPACES)
def test_candidate_namespace_real_source_missing_scope_is_detected(
    tmp_path, namespace_case
) -> None:
    source_path, identifier, _extra_kind = namespace_case
    app_root, target = _copy_candidate_namespace(tmp_path, source_path)
    tree = ast.parse(target.read_text(encoding="utf-8"))
    changed = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == identifier:
            node.id = "broken_namespace_scope"
            changed += 1
        elif isinstance(node, ast.Attribute) and node.attr == identifier:
            node.attr = "broken_namespace_scope"
            changed += 1
    assert changed > 0
    target.write_text(ast.unparse(tree), encoding="utf-8")

    result = scan_source_boundaries(app_root)

    assert (
        IsolationAuditIssue("TENANT_NAMESPACE_REGISTRY_MISMATCH", source_path, 1)
        in result.issues
    )
    assert result.namespace_checks["validCount"] == 0


@pytest.mark.parametrize("namespace_case", _CANDIDATE_NAMESPACES)
def test_candidate_namespace_real_source_new_dependency_is_detected(
    tmp_path, namespace_case
) -> None:
    source_path, _identifier, extra_kind = namespace_case
    app_root, target = _copy_candidate_namespace(tmp_path, source_path)
    added_import = (
        "from app.core.file_storage import FileStorage\n"
        if extra_kind == "storage"
        else "from app.core.redis import redis_client\n"
    )
    target.write_text(
        target.read_text(encoding="utf-8") + "\n" + added_import,
        encoding="utf-8",
    )

    result = scan_source_boundaries(app_root)

    assert (
        IsolationAuditIssue("TENANT_NAMESPACE_REGISTRY_MISMATCH", source_path, 1)
        in result.issues
    )


@pytest.mark.parametrize("namespace_case", _CANDIDATE_NAMESPACES)
def test_candidate_namespace_registration_does_not_cover_other_paths(
    tmp_path, namespace_case
) -> None:
    source_path, _identifier, _extra_kind = namespace_case
    app_root, target = _copy_candidate_namespace(tmp_path, source_path)
    unregistered = target.with_name("unregistered_candidate.py")
    target.rename(unregistered)

    result = scan_source_boundaries(app_root)

    assert (
        IsolationAuditIssue(
            "TENANT_NAMESPACE_SOURCE_UNREGISTERED",
            unregistered.relative_to(app_root).as_posix(),
            1,
        )
        in result.issues
    )


def test_source_scan_detects_hardcoded_tenant_and_unscoped_storage(tmp_path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "unsafe.py").write_text(
        "async def bad(storage):\n"
        "    tenant_id = 0\n"
        "    await storage.save(b'x', mime_type='x', namespace='preview')\n",
        encoding="utf-8",
    )

    result = scan_source_boundaries(app_root)

    codes = {issue.code for issue in result.issues}
    assert "TENANT_HARDCODED_ZERO" in codes
    assert "TENANT_STORAGE_NAMESPACE_MISSING" in codes
    assert result.source_digest


def test_source_scan_detects_tenant_zero_function_defaults(tmp_path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "unsafe.py").write_text(
        "def positional(tenant_id=0):\n"
        "    return tenant_id\n"
        "def keyword(*, tenantId=0):\n"
        "    return tenantId\n",
        encoding="utf-8",
    )

    result = scan_source_boundaries(app_root)

    issue = next(item for item in result.issues if item.code == "TENANT_HARDCODED_ZERO")
    assert issue.count == 2


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (
            "from app.core.redis import redis_client\n"
            "async def bad():\n"
            "    await redis_client.set('shared', 'value')\n",
            "TENANT_NAMESPACE_SOURCE_UNREGISTERED",
        ),
        (
            "from app.core.cache import cache_get\n"
            "async def bad():\n"
            "    return await cache_get('shared')\n",
            "TENANT_NAMESPACE_SOURCE_UNREGISTERED",
        ),
        (
            "from app.core.file_storage import FileStorage\n"
            "async def bad(storage: FileStorage):\n"
            "    await storage.save(b'x', mime_type='text/plain')\n",
            "TENANT_NAMESPACE_SOURCE_UNREGISTERED",
        ),
        (
            "_cache: dict[str, str] = {}\n",
            "TENANT_NAMESPACE_SOURCE_UNREGISTERED",
        ),
    ],
)
def test_source_scan_fails_closed_for_new_namespace_sources(
    tmp_path, source: str, expected_code: str
) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "new_boundary.py").write_text(source, encoding="utf-8")

    result = scan_source_boundaries(app_root)

    assert expected_code in {issue.code for issue in result.issues}


def test_report_hash_and_risk_order_are_deterministic() -> None:
    issues = [
        IsolationAuditIssue("Z_LAST", "z.py", 2),
        IsolationAuditIssue("A_FIRST", "a.py", 1),
    ]
    first = TenantIsolationAuditReport.from_parts(
        build_sha="abcdef1",
        source_digest="1" * 64,
        schema_digest="2" * 64,
        inventory={"tenantOwnedCount": 1, "platformGlobalCount": 1},
        database_checks={"legacyScopedMatch": False},
        namespace_checks={"registeredCount": 0},
        issues=issues,
    )
    second = TenantIsolationAuditReport.from_parts(
        build_sha="abcdef1",
        source_digest="1" * 64,
        schema_digest="2" * 64,
        inventory={"platformGlobalCount": 1, "tenantOwnedCount": 1},
        database_checks={"legacyScopedMatch": False},
        namespace_checks={"registeredCount": 0},
        issues=list(reversed(issues)),
    )

    assert first.as_dict() == second.as_dict()
    assert [item["code"] for item in first.payload["risks"]] == [
        "A_FIRST",
        "Z_LAST",
    ]


def test_release_script_binds_report_to_checked_out_build(monkeypatch) -> None:
    def completed(command, **_kwargs):
        output = "abcdef1234567890\n" if command[1] == "rev-parse" else ""
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        completed,
    )

    assert cli._verified_build_sha("abcdef1") == "abcdef1234567890"
    with pytest.raises(ValueError, match="does not match"):
        cli._verified_build_sha("1234567")


def test_release_script_rejects_dirty_checkout(monkeypatch) -> None:
    def completed(command, **_kwargs):
        output = (
            "abcdef1234567890\n" if command[1] == "rev-parse" else " M app/main.py\n"
        )
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(cli.subprocess, "run", completed)

    with pytest.raises(ValueError, match="clean Git checkout"):
        cli._verified_build_sha("abcdef1")
