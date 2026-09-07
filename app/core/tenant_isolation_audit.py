"""Deterministic, read-only tenant isolation release audit."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import ForeignKeyConstraint, and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_inventory import (
    HOSTED_CONTAINED_TABLES,
    INFRASTRUCTURE_TABLES,
    PLATFORM_GLOBAL_TABLES,
    TENANT_MODEL_INVENTORY,
    TenantNullability,
    load_inventory_table,
)

_BUILD_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_APP_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TENANT_REFERENCE_ALLOWLIST = frozenset(
    {
        "core/tenant.py",
        "modules/auth/service.py",
        "modules/marketplace/capability.py",
        "modules/system/service/tenant_bootstrap_service.py",
        "modules/system/service/tenant_lifecycle_service.py",
    }
)
_NAMESPACE_REGISTRY: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "main.py": ("cache_storage", "infrastructure", ()),
    "scheduler_worker.py": ("cache", "infrastructure", ()),
    "core/cache.py": ("cache", "infrastructure", ()),
    "core/file_storage.py": ("storage", "infrastructure", ()),
    "core/redis.py": ("cache", "infrastructure", ()),
    "core/scheduler.py": ("cache", "infrastructure", ()),
    "core/tenant_scope.py": ("cache", "tenant", ("tenant_id",)),
    "middleware/audit_middleware.py": ("cache", "tenant", ("tenant_id",)),
    "modules/auth/service.py": ("cache", "global", ()),
    "modules/ai/lifecycle.py": ("cache", "tenant", ("tenant_id",)),
    "modules/ai/api/chat.py": ("cache", "tenant", ("tenant",)),
    "modules/ai/api/confirm.py": ("cache", "tenant", ("tenant_id",)),
    "modules/ai/api/query_cache.py": (
        "cache",
        "tenant",
        ("get_bound_tenant_context",),
    ),
    "modules/ai/api/resume.py": ("cache", "tenant", ("tenant_id",)),
    "modules/ai/agents/gateway/executor.py": ("cache", "tenant", ("tenant",)),
    "modules/ai/agents/gateway/failures.py": ("cache", "tenant", ("tenant_id",)),
    "modules/ai/agents/gateway/quota.py": ("cache", "tenant", ("tenant_id",)),
    "modules/ai/agents/hitl/manager.py": ("cache", "tenant", ("tenant_id",)),
    "modules/ai/agents/hitl/query_cache.py": ("cache", "tenant", ("tenant_id",)),
    "modules/ai/agents/safety/auto_disable.py": (
        "cache",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/agents/safety/ai_config.py": (
        "memory",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/agents/safety/forbidden_topics.py": (
        "memory",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/agents/safety/forbidden_urls.py": (
        "memory",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/agents/safety/injection_detector.py": (
        "cache",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/agents/safety/ip_blacklist.py": (
        "cache_memory",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/agents/safety/keyword_blocklist.py": (
        "memory",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/agents/supervisor/quota.py": (
        "cache",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/service/chat_run_service.py": (
        "cache",
        "tenant",
        ("tenant_id",),
    ),
    "modules/ai/service/prepared_action_service.py": (
        "storage",
        "tenant",
        ("tenant_id",),
    ),
    "modules/marketplace/service/contributes_service.py": (
        "cache",
        "contained",
        ("require_marketplace_capability",),
    ),
    "modules/marketplace/service/upload_service.py": (
        "storage",
        "contained",
        ("require_marketplace_capability",),
    ),
    "modules/system/service/config_service.py": (
        "cache",
        "tenant",
        ("tenant_id",),
    ),
    "modules/system/api/config.py": (
        "cache",
        "tenant",
        ("tenant_id",),
    ),
    "modules/system/service/user_import_service.py": (
        "cache_storage",
        "tenant",
        ("tenant_id",),
    ),
    "modules/system/service/user_service.py": (
        "cache",
        "tenant",
        ("tenant_cache_key",),
    ),
    "modules/system/service/user_export_service.py": (
        "storage",
        "tenant",
        ("tenant_id",),
    ),
    "modules/system/service/file_service.py": (
        "storage",
        "tenant",
        ("tenant_id",),
    ),
    "modules/system/ai_tools/user_transfer.py": (
        "storage",
        "tenant",
        ("tenant_id",),
    ),
    "utils/storage.py": ("storage", "infrastructure", ()),
}

_REQUIRED_SECURITY_TRIGGERS = {
    ("sys_platform_principal", "trg_platform_principal_security_version"): (
        "bump_platform_principal_security_version",
        (
            "NEW.hashed_password IS DISTINCT FROM OLD.hashed_password",
            "NEW.status IS DISTINCT FROM OLD.status",
            "NEW.permissions IS DISTINCT FROM OLD.permissions",
            "NEW.row_version := OLD.row_version + 1",
        ),
    ),
    ("sys_platform_audit_log", "trg_platform_audit_validate_lineage"): (
        "validate_platform_audit_lineage",
        (
            "NEW.authorization_audit_id",
            "authorized.actor_principal_id = NEW.actor_principal_id",
            "authorized.permission = NEW.permission",
            "authorized.correlation_id = NEW.correlation_id",
        ),
    ),
    ("sys_platform_audit_log", "trg_platform_audit_append_only"): (
        "reject_platform_audit_mutation",
        ("sys_platform_audit_log is append-only",),
    ),
    ("sys_tenant", "trg_sys_tenant_security_version"): (
        "bump_sys_tenant_security_version",
        (
            "NEW.status IS DISTINCT FROM OLD.status",
            "NEW.lifecycle_state IS DISTINCT FROM OLD.lifecycle_state",
            "NEW.row_version := OLD.row_version + 1",
        ),
    ),
}
_LOGICAL_RELATIONSHIPS: dict[
    tuple[str, tuple[str, ...]], tuple[str, tuple[str, ...], bool]
] = {
    ("ai_routing_feedback", ("tenant_id", "message_id")): (
        "ai_message",
        ("tenant_id", "message_id"),
        True,
    )
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class IsolationAuditIssue:
    code: str
    resource: str
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "resource": self.resource, "count": self.count}


@dataclass(frozen=True, slots=True)
class SourceBoundaryScan:
    source_digest: str
    namespace_checks: dict[str, Any]
    issues: tuple[IsolationAuditIssue, ...]


@dataclass(frozen=True, slots=True)
class TenantIsolationAuditReport:
    payload: dict[str, Any]
    report_sha256: str
    risk_count: int

    @classmethod
    def from_parts(
        cls,
        *,
        build_sha: str,
        source_digest: str,
        schema_digest: str,
        inventory: dict[str, Any],
        database_checks: dict[str, Any],
        namespace_checks: dict[str, Any],
        issues: list[IsolationAuditIssue],
        schema_revision: str | None = None,
    ) -> TenantIsolationAuditReport:
        ordered = sorted(
            issues, key=lambda item: (item.code, item.resource, item.count)
        )
        payload = {
            "schemaVersion": 1,
            "buildSha": build_sha.lower(),
            "schemaRevision": schema_revision,
            "sourceDigest": source_digest,
            "schemaDigest": schema_digest,
            "modelInventory": inventory,
            "databaseChecks": database_checks,
            "namespaceChecks": namespace_checks,
            "riskCount": sum(item.count for item in ordered),
            "risks": [item.as_dict() for item in ordered],
        }
        return cls(
            payload=payload,
            report_sha256=_digest(payload),
            risk_count=payload["riskCount"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload, "reportSha256": self.report_sha256}


def _is_zero(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Constant) and value.value == 0


def _target_is_tenant_id(target: ast.AST) -> bool:
    return isinstance(target, ast.Name) and target.id in {"tenant_id", "tenantId"}


def _function_default_zero_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    positional = [*node.args.posonlyargs, *node.args.args]
    defaulted = positional[-len(node.args.defaults) :] if node.args.defaults else []
    count = sum(
        argument.arg in {"tenant_id", "tenantId"} and _is_zero(default)
        for argument, default in zip(defaulted, node.args.defaults, strict=True)
    )
    count += sum(
        argument.arg in {"tenant_id", "tenantId"} and _is_zero(default)
        for argument, default in zip(
            node.args.kwonlyargs, node.args.kw_defaults, strict=True
        )
    )
    return count


def _hardcoded_zero_count(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if _is_zero(node.value) and any(
                _target_is_tenant_id(item) for item in targets
            ):
                count += 1
        elif isinstance(node, ast.Call):
            count += sum(
                1
                for keyword in node.keywords
                if keyword.arg in {"tenant_id", "tenantId"} and _is_zero(keyword.value)
            )
        elif isinstance(node, ast.Dict):
            count += sum(
                1
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant)
                and key.value in {"tenant_id", "tenantId"}
                and _is_zero(value)
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count += _function_default_zero_count(node)
    return count


def _storage_namespace_issues(
    tree: ast.AST, source: str, relative_path: str, *, storage_source: bool
) -> list[IsolationAuditIssue]:
    issues = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "save"
        ):
            continue
        namespace = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "namespace"),
            None,
        )
        if namespace is None:
            receiver = node.func.value
            if not (
                storage_source
                and isinstance(receiver, ast.Name)
                and receiver.id in {"storage", "file_storage"}
            ):
                continue
        expression = ast.get_source_segment(source, namespace) if namespace else ""
        if "tenant" not in expression or "tenant_id" not in expression:
            issues.append(
                IsolationAuditIssue(
                    "TENANT_STORAGE_NAMESPACE_MISSING", relative_path, 1
                )
            )
    return issues


def _namespace_source_kinds(tree: ast.AST) -> frozenset[str]:
    """Discover namespace-bearing dependencies from syntax, not comments."""
    kinds: set[str] = set()
    for node in getattr(tree, "body", ()):  # only module-level process caches
        target = None
        value = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and target.id.endswith("cache")
            and isinstance(value, (ast.Dict, ast.Call))
        ):
            kinds.add("memory")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "redis" or alias.name.startswith("redis.")
                for alias in node.names
            ):
                kinds.add("cache")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in {
                "redis",
                "app.core.cache",
                "app.core.redis",
            } or module.startswith("redis."):
                kinds.add("cache")
            if module == "app.core" and any(
                alias.name == "redis" for alias in node.names
            ):
                kinds.add("cache")
            if module in {"app.core.file_storage", "app.utils.storage"}:
                kinds.add("storage")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) > 20 and "redis.call(" in node.value:
                kinds.add("cache")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in {"FileStorage", "LocalFileStorage", "save_file"}:
                kinds.add("storage")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "save"
            and any(keyword.arg == "namespace" for keyword in node.keywords)
        ):
            kinds.add("storage")
    return frozenset(kinds)


def _semantic_identifiers(tree: ast.AST) -> frozenset[str]:
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    return frozenset(identifiers)


def scan_source_boundaries(app_root: Path = _APP_ROOT) -> SourceBoundaryScan:
    issues: list[IsolationAuditIssue] = []
    digest_input: list[tuple[str, str]] = []
    discovered: dict[str, frozenset[str]] = {}
    for path in sorted(app_root.rglob("*.py")):
        relative_path = path.relative_to(app_root).as_posix()
        source = path.read_text(encoding="utf-8")
        digest_input.append(
            (relative_path, hashlib.sha256(source.encode()).hexdigest())
        )
        tree = ast.parse(source, filename=relative_path)
        kinds = _namespace_source_kinds(tree)
        if kinds:
            discovered[relative_path] = kinds
        hardcoded = _hardcoded_zero_count(tree)
        if hardcoded:
            issues.append(
                IsolationAuditIssue("TENANT_HARDCODED_ZERO", relative_path, hardcoded)
            )
        has_default_tenant_reference = any(
            isinstance(node, ast.Name) and node.id == "DEFAULT_TENANT_ID"
            for node in ast.walk(tree)
        )
        if has_default_tenant_reference and (
            relative_path not in _DEFAULT_TENANT_REFERENCE_ALLOWLIST
        ):
            issues.append(
                IsolationAuditIssue(
                    "TENANT_DEFAULT_REFERENCE_UNREGISTERED", relative_path, 1
                )
            )
        issues.extend(
            _storage_namespace_issues(
                tree,
                source,
                relative_path,
                storage_source="storage" in kinds,
            )
        )

    for relative_path in sorted(set(discovered) - set(_NAMESPACE_REGISTRY)):
        issues.append(
            IsolationAuditIssue(
                "TENANT_NAMESPACE_SOURCE_UNREGISTERED", relative_path, 1
            )
        )

    registry_records = []
    for relative_path, (kind, scope, required_identifiers) in sorted(
        _NAMESPACE_REGISTRY.items()
    ):
        path = app_root / relative_path
        present = path.is_file()
        if not present:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        identifiers = _semantic_identifiers(tree)
        actual_kinds = _namespace_source_kinds(tree)
        declared_kinds = set(kind.split("_"))
        valid = actual_kinds.issubset(declared_kinds) and all(
            identifier in identifiers for identifier in required_identifiers
        )
        registry_records.append(
            {
                "source": relative_path,
                "kind": kind,
                "scope": scope,
                "semanticRequirementsDigest": _digest(list(required_identifiers)),
                "valid": valid,
            }
        )
        if not valid:
            issues.append(
                IsolationAuditIssue(
                    "TENANT_NAMESPACE_REGISTRY_MISMATCH", relative_path, 1
                )
            )
    return SourceBoundaryScan(
        source_digest=_digest(digest_input),
        namespace_checks={
            "registeredCount": len(registry_records),
            "validCount": sum(item["valid"] for item in registry_records),
            "registryDigest": _digest(registry_records),
        },
        issues=tuple(issues),
    )


def _schema_snapshot(sync_connection) -> dict[str, Any]:  # noqa: ANN001
    from sqlalchemy import inspect  # noqa: PLC0415

    inspector = inspect(sync_connection)
    table_names = set(inspector.get_table_names())
    classified = sorted(
        set(TENANT_MODEL_INVENTORY)
        | set(PLATFORM_GLOBAL_TABLES)
        | set(HOSTED_CONTAINED_TABLES)
    )
    tables = {}
    for table_name in classified:
        if table_name not in table_names:
            continue
        primary_key = inspector.get_pk_constraint(table_name)
        tables[table_name] = {
            "columns": [
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": column["nullable"],
                    "default": column.get("default"),
                }
                for column in inspector.get_columns(table_name)
            ],
            "primaryKey": {
                "name": primary_key.get("name"),
                "columns": primary_key.get("constrained_columns") or [],
            },
            "uniqueConstraints": [
                {
                    "name": item.get("name"),
                    "columns": item.get("column_names") or [],
                }
                for item in inspector.get_unique_constraints(table_name)
            ],
            "foreignKeys": [
                {
                    "name": item.get("name"),
                    "columns": item.get("constrained_columns") or [],
                    "targetTable": item.get("referred_table"),
                    "targetColumns": item.get("referred_columns") or [],
                    "onDelete": (item.get("options") or {}).get("ondelete"),
                }
                for item in inspector.get_foreign_keys(table_name)
            ],
            "indexes": [
                {
                    "name": item.get("name"),
                    "columns": item.get("column_names") or [],
                    "unique": bool(item.get("unique")),
                }
                for item in inspector.get_indexes(table_name)
            ],
            "checkConstraints": [
                {"name": item.get("name"), "sql": item.get("sqltext")}
                for item in inspector.get_check_constraints(table_name)
            ],
        }
    tenant_column_tables = sorted(
        table_name
        for table_name in table_names
        if any(
            column["name"] == "tenant_id"
            for column in inspector.get_columns(table_name)
        )
    )
    trigger_rows = sync_connection.execute(
        text(
            """
            SELECT c.relname AS table_name,
                   t.tgname AS trigger_name,
                   pg_get_triggerdef(t.oid) AS trigger_definition,
                   p.proname AS function_name,
                   pg_get_functiondef(p.oid) AS function_definition
              FROM pg_trigger AS t
              JOIN pg_class AS c ON c.oid = t.tgrelid
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
              JOIN pg_proc AS p ON p.oid = t.tgfoid
             WHERE NOT t.tgisinternal
               AND n.nspname = current_schema()
             ORDER BY c.relname, t.tgname
            """
        )
    ).mappings()
    triggers = [
        {
            "table": row["table_name"],
            "name": row["trigger_name"],
            "definition": " ".join(row["trigger_definition"].split()),
            "function": row["function_name"],
            "functionDefinition": " ".join(row["function_definition"].split()),
        }
        for row in trigger_rows
    ]
    return {
        "allTables": sorted(table_names),
        "tables": tables,
        "tenantColumnTables": tenant_column_tables,
        "triggers": triggers,
    }


async def _identity_snapshot(
    db: AsyncSession, statement, *, table_name: str
) -> tuple[int, str]:  # noqa: ANN001
    """Hash ordered row identities without retaining the table contents in memory."""
    digest = hashlib.sha256()
    digest.update(b"[")
    count = 0
    result = await db.stream(statement)
    async for row in result:
        if count:
            digest.update(b",")
        identity = [
            table_name,
            *(None if value is None else str(value) for value in row),
        ]
        digest.update(_canonical_json(identity))
        count += 1
    digest.update(b"]")
    return count, digest.hexdigest()


async def _relationship_counts(
    db: AsyncSession,
    *,
    table_name: str,
    local_columns: tuple[str, ...],
) -> tuple[int, int]:
    table = load_inventory_table(TENANT_MODEL_INVENTORY[table_name])
    constraint = next(
        (
            item
            for item in table.foreign_key_constraints
            if isinstance(item, ForeignKeyConstraint)
            and tuple(item.column_keys) == local_columns
        ),
        None,
    )
    allow_orphan = False
    if constraint is not None:
        target_table = next(iter(constraint.elements)).column.table
        remote_columns = tuple(element.column.name for element in constraint.elements)
    else:
        logical = _LOGICAL_RELATIONSHIPS.get((table_name, local_columns))
        if logical is None:
            return 0, 1
        target_name, remote_columns, allow_orphan = logical
        target_table = load_inventory_table(TENANT_MODEL_INVENTORY[target_name])

    local = table.alias(f"audit_local_{table_name}")
    target = target_table.alias(f"audit_target_{target_table.name}")
    identity_present = and_(
        *(
            target.c[remote] == local.c[source]
            for source, remote in zip(
                local_columns[1:], remote_columns[1:], strict=True
            )
        )
    )
    exact_present = and_(
        *(
            target.c[remote] == local.c[source]
            for source, remote in zip(local_columns, remote_columns, strict=True)
        )
    )
    identity_values_present = and_(
        *(local.c[name].is_not(None) for name in local_columns[1:])
    )
    has_identity = exists(select(1).select_from(target).where(identity_present))
    has_exact = exists(select(1).select_from(target).where(exact_present))
    cross_count = (
        await db.scalar(
            select(func.count())
            .select_from(local)
            .where(identity_values_present, ~has_exact, has_identity)
        )
        or 0
    )
    orphan_count = 0
    if not allow_orphan:
        orphan_count = (
            await db.scalar(
                select(func.count())
                .select_from(local)
                .where(identity_values_present, ~has_identity)
            )
            or 0
        )
    return cross_count, orphan_count


def _schema_contract_issues(schema: dict[str, Any]) -> list[IsolationAuditIssue]:
    issues: list[IsolationAuditIssue] = []
    actual_triggers = {
        (item["table"], item["name"]): item for item in schema["triggers"]
    }
    for (table_name, trigger_name), (function_name, markers) in sorted(
        _REQUIRED_SECURITY_TRIGGERS.items()
    ):
        actual = actual_triggers.get((table_name, trigger_name))
        resource = f"{table_name}.{trigger_name}"
        if actual is None:
            issues.append(
                IsolationAuditIssue("TENANT_SCHEMA_TRIGGER_MISSING", resource, 1)
            )
            continue
        function_definition = actual["functionDefinition"]
        if actual["function"] != function_name or not all(
            marker in function_definition for marker in markers
        ):
            issues.append(
                IsolationAuditIssue("TENANT_SCHEMA_TRIGGER_MISMATCH", resource, 1)
            )

    for table_name, resource in sorted(TENANT_MODEL_INVENTORY.items()):
        actual = schema["tables"].get(table_name)
        if actual is None:
            continue
        columns = {item["name"]: item for item in actual["columns"]}
        tenant_column = columns.get("tenant_id")
        expected_nullable = resource.nullability is TenantNullability.AUDIT_OPTIONAL
        if tenant_column is None or tenant_column["nullable"] is not expected_nullable:
            issues.append(
                IsolationAuditIssue(
                    "TENANT_SCHEMA_COLUMN_MISMATCH", f"{table_name}.tenant_id", 1
                )
            )

        unique_signatures = {
            tuple(item["columns"]) for item in actual["uniqueConstraints"]
        } | {tuple(item["columns"]) for item in actual["indexes"] if item["unique"]}
        primary_columns = tuple(actual["primaryKey"]["columns"])
        if primary_columns:
            unique_signatures.add(primary_columns)
        for unique_key in resource.unique_keys:
            if unique_key not in unique_signatures:
                issues.append(
                    IsolationAuditIssue(
                        "TENANT_SCHEMA_UNIQUE_MISSING",
                        f"{table_name}.{'_'.join(unique_key)}",
                        1,
                    )
                )

        index_signatures = [
            tuple(item["columns"]) for item in actual["indexes"] if item["columns"]
        ]
        index_signatures.extend(unique_signatures)
        if primary_columns:
            index_signatures.append(primary_columns)
        if not any(
            columns and columns[0] == "tenant_id" for columns in index_signatures
        ):
            issues.append(
                IsolationAuditIssue("TENANT_SCHEMA_INDEX_MISSING", table_name, 1)
            )

        actual_foreign_keys = {tuple(item["columns"]) for item in actual["foreignKeys"]}
        table = load_inventory_table(resource)
        orm_foreign_keys = {
            tuple(constraint.column_keys)
            for constraint in table.foreign_key_constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        for relationship in resource.relationship_keys:
            if (
                relationship in orm_foreign_keys
                and relationship not in actual_foreign_keys
            ):
                issues.append(
                    IsolationAuditIssue(
                        "TENANT_SCHEMA_FOREIGN_KEY_MISSING",
                        f"{table_name}.{'_'.join(relationship)}",
                        1,
                    )
                )
    return issues


async def _hosted_containment_violation_count(
    db: AsyncSession, *, table_name: str
) -> int:
    resource = HOSTED_CONTAINED_TABLES[table_name]
    table = load_inventory_table(resource)
    conditions = []
    if resource.tenant_column is not None:
        conditions.append(
            table.c[resource.tenant_column].not_in(resource.allowed_tenant_ids)
        )
    if resource.parent_table is not None and resource.parent_column is not None:
        parent_resource = HOSTED_CONTAINED_TABLES[resource.parent_table]
        if parent_resource.tenant_column is None:
            return 1
        parent = load_inventory_table(parent_resource).alias(
            f"audit_containment_{resource.parent_table}"
        )
        parent_is_allowed = exists(
            select(1)
            .select_from(parent)
            .where(
                parent.c.id == table.c[resource.parent_column],
                parent.c[parent_resource.tenant_column].in_(
                    resource.allowed_tenant_ids
                ),
            )
        )
        conditions.append(~parent_is_allowed)
    if resource.user_columns:
        user = load_inventory_table(TENANT_MODEL_INVENTORY["sys_user"]).alias(
            f"audit_containment_user_{table_name}"
        )
        for column_name in resource.user_columns:
            user_id = table.c[column_name]
            user_is_allowed = exists(
                select(1)
                .select_from(user)
                .where(
                    user.c.user_id == user_id,
                    user.c.tenant_id.in_(resource.allowed_tenant_ids),
                )
            )
            conditions.append(user_id.is_not(None) & ~user_is_allowed)
    if not conditions:
        return 1
    return (
        await db.scalar(select(func.count()).select_from(table).where(or_(*conditions)))
        or 0
    )


async def build_tenant_isolation_report(
    db: AsyncSession,
    *,
    build_sha: str,
    app_root: Path = _APP_ROOT,
) -> TenantIsolationAuditReport:
    if _BUILD_SHA_RE.fullmatch(build_sha) is None:
        raise ValueError("build_sha must be a 7-64 character hexadecimal Git SHA")
    source_scan = scan_source_boundaries(app_root)
    issues = list(source_scan.issues)

    for resource in (
        *TENANT_MODEL_INVENTORY.values(),
        *PLATFORM_GLOBAL_TABLES.values(),
        *HOSTED_CONTAINED_TABLES.values(),
    ):
        load_inventory_table(resource)
    connection = await db.connection()
    schema = await connection.run_sync(_schema_snapshot)
    schema_revisions = tuple(
        (
            await db.scalars(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            )
        ).all()
    )
    schema_revision = schema_revisions[0] if len(schema_revisions) == 1 else None
    schema_digest = _digest({"revision": schema_revision, **schema})
    classified = (
        set(TENANT_MODEL_INVENTORY)
        | set(PLATFORM_GLOBAL_TABLES)
        | set(HOSTED_CONTAINED_TABLES)
    )
    if len(schema_revisions) != 1:
        issues.append(
            IsolationAuditIssue(
                "ALEMBIC_HEAD_COUNT_INVALID", "alembic_version", len(schema_revisions)
            )
        )
    from app.db.base import Base  # noqa: PLC0415

    orm_tables = {
        table_name
        for table_name, table in Base.metadata.tables.items()
        if table.info.get("tenant_isolation_audit_exempt") != "test-only"
    }
    for table_name in sorted(orm_tables - classified):
        issues.append(
            IsolationAuditIssue("TENANT_ORM_MODEL_UNCLASSIFIED", table_name, 1)
        )
    allowed_physical_tables = classified | set(INFRASTRUCTURE_TABLES)
    for table_name in sorted(set(schema["allTables"]) - allowed_physical_tables):
        issues.append(IsolationAuditIssue("TENANT_MODEL_UNCLASSIFIED", table_name, 1))
    for table_name in sorted(classified - set(schema["tables"])):
        issues.append(IsolationAuditIssue("TENANT_MODEL_TABLE_MISSING", table_name, 1))
    issues.extend(_schema_contract_issues(schema))

    tenant_table = load_inventory_table(PLATFORM_GLOBAL_TABLES["sys_tenant"])
    valid_tenant_ids = select(tenant_table.c.tenant_id)
    containment_records = []
    for table_name in sorted(HOSTED_CONTAINED_TABLES):
        violation_count = await _hosted_containment_violation_count(
            db, table_name=table_name
        )
        if violation_count:
            issues.append(
                IsolationAuditIssue(
                    "HOSTED_CONTAINMENT_TENANT_VIOLATION",
                    table_name,
                    violation_count,
                )
            )
        containment_records.append(
            {"table": table_name, "violationCount": violation_count}
        )
    resources: list[dict[str, Any]] = []
    all_matches = True
    for table_name, resource in sorted(TENANT_MODEL_INVENTORY.items()):
        table = load_inventory_table(resource)
        null_count = (
            await db.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.tenant_id.is_(None))
            )
            or 0
        )
        if null_count and resource.nullability is TenantNullability.REQUIRED:
            issues.append(
                IsolationAuditIssue("TENANT_REQUIRED_NULL", table_name, null_count)
            )
        orphan_tenants = (
            await db.scalar(
                select(func.count())
                .select_from(table)
                .where(
                    table.c.tenant_id.is_not(None),
                    table.c.tenant_id.not_in(valid_tenant_ids),
                )
            )
            or 0
        )
        if orphan_tenants:
            issues.append(
                IsolationAuditIssue(
                    "TENANT_REGISTRY_ORPHAN", table_name, orphan_tenants
                )
            )

        unique_conflicts = 0
        for columns in resource.unique_keys:
            group_columns = [table.c[name] for name in columns]
            statement = (
                select(func.count())
                .select_from(table)
                .where(*(column.is_not(None) for column in group_columns))
                .group_by(*group_columns)
                .having(func.count() > 1)
            )
            unique_conflicts += len((await db.execute(statement)).all())
        if unique_conflicts:
            issues.append(
                IsolationAuditIssue(
                    "TENANT_UNIQUE_CONFLICT", table_name, unique_conflicts
                )
            )

        cross_count = 0
        relationship_orphans = 0
        for relationship in resource.relationship_keys:
            cross, orphan = await _relationship_counts(
                db, table_name=table_name, local_columns=relationship
            )
            cross_count += cross
            relationship_orphans += orphan
        if cross_count:
            issues.append(
                IsolationAuditIssue("TENANT_CROSS_LINK", table_name, cross_count)
            )
        if relationship_orphans:
            issues.append(
                IsolationAuditIssue(
                    "TENANT_RELATIONSHIP_ORPHAN", table_name, relationship_orphans
                )
            )

        primary_keys = list(table.primary_key.columns)
        statement = select(table.c.tenant_id, *primary_keys).order_by(
            table.c.tenant_id.asc().nulls_first(),
            *(column.asc() for column in primary_keys),
        )
        legacy_count, legacy_digest = await _identity_snapshot(
            db, statement, table_name=table_name
        )
        scoped_filter = table.c.tenant_id.in_(valid_tenant_ids)
        if resource.nullability is TenantNullability.AUDIT_OPTIONAL:
            scoped_filter = scoped_filter | table.c.tenant_id.is_(None)
        scoped_count, scoped_digest = await _identity_snapshot(
            db, statement.where(scoped_filter), table_name=table_name
        )
        matches = legacy_count == scoped_count and legacy_digest == scoped_digest
        all_matches = all_matches and matches
        if not matches:
            issues.append(
                IsolationAuditIssue("TENANT_SCOPED_DIGEST_MISMATCH", table_name, 1)
            )
        resources.append(
            {
                "table": table_name,
                "legacyCount": legacy_count,
                "scopedCount": scoped_count,
                "legacyDigest": legacy_digest,
                "scopedDigest": scoped_digest,
                "nullCount": null_count,
                "tenantOrphanCount": orphan_tenants,
                "crossLinkCount": cross_count,
                "relationshipOrphanCount": relationship_orphans,
                "uniqueConflictCount": unique_conflicts,
                "matches": matches,
            }
        )

    return TenantIsolationAuditReport.from_parts(
        build_sha=build_sha,
        source_digest=source_scan.source_digest,
        schema_digest=schema_digest,
        schema_revision=schema_revision,
        inventory={
            "tenantOwnedCount": len(TENANT_MODEL_INVENTORY),
            "platformGlobalCount": len(PLATFORM_GLOBAL_TABLES),
            "hostedContainedCount": len(HOSTED_CONTAINED_TABLES),
            "hostedContainmentDigest": _digest(containment_records),
            "inventoryDigest": _digest(
                {
                    "tenantOwned": sorted(TENANT_MODEL_INVENTORY),
                    "platformGlobal": sorted(PLATFORM_GLOBAL_TABLES),
                    "hostedContained": sorted(HOSTED_CONTAINED_TABLES),
                }
            ),
        },
        database_checks={
            "legacyScopedMatch": all_matches,
            "resourceCount": len(resources),
            "resourcesDigest": _digest(resources),
            "resources": resources,
        },
        namespace_checks=source_scan.namespace_checks,
        issues=issues,
    )
