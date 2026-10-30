"""Pure AST coverage gate for authorization contracts; no ingress certification."""

import ast
from pathlib import Path

from app.core.authorization_candidates import (
    CANDIDATES,
    IO_PROFILES,
    MAINTENANCE_ENTRIES,
    OPERATION_FAMILIES,
)

ROOT = Path(__file__).resolve().parents[2]


def check_candidates(*, root=ROOT, entries=None, module_sources=None):
    entries = CANDIDATES if entries is None else entries
    module_sources = module_sources or {}
    parsed, errors = {}, []

    def tree(module):
        if module not in parsed:
            path = root.joinpath(*module.split(".")).with_suffix(".py")
            source = module_sources.get(module)
            if source is None:
                source = path.read_text(encoding="utf-8-sig")
            parsed[module] = ast.parse(source, filename=str(path))
        return parsed[module]

    def resolve(qualified):
        parts = qualified.split(".")
        for end in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:end])
            path = root.joinpath(*parts[:end]).with_suffix(".py")
            if module not in module_sources and not path.is_file():
                continue
            node = tree(module)
            for name in parts[end:]:
                node = next(
                    (
                        item
                        for item in node.body
                        if isinstance(
                            item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                        )
                        and item.name == name
                    ),
                    None,
                )
                if node is None:
                    return None
            return (
                node
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else None
            )
        return None

    seen, covered = set(), set()
    operations = {family: set() for family in OPERATION_FAMILIES}
    for entry in entries:
        if entry.entry_id in seen:
            errors.append(f"duplicate entry_id: {entry.entry_id}")
        seen.add(entry.entry_id)
        if entry.io_profile not in IO_PROFILES:
            errors.append(f"{entry.entry_id}: unknown io_profile")
        if entry.phase not in {"owner", "proof_writer", "plan", "read", "external"}:
            errors.append(f"{entry.entry_id}: unknown phase")
        if entry.G_mode not in {"none", "S", "X"} or any(
            mode not in {"S", "X"} for mode in entry.B_modes
        ):
            errors.append(f"{entry.entry_id}: unknown barrier mode")
        if entry.production_connected is not False:
            errors.append(
                f"{entry.entry_id}: unsupported production_connected/maintenance claim"
            )
        if entry.maintenance != (entry.entry_id in MAINTENANCE_ENTRIES):
            errors.append(f"{entry.entry_id}: maintenance fence inventory mismatch")
        if entry.phase == "external" and (entry.G_mode != "none" or entry.B_modes):
            errors.append(f"{entry.entry_id}: external IO cannot hold barriers")
        if entry.phase == "proof_writer" and not entry.proof_only:
            errors.append(f"{entry.entry_id}: proof writer must require proof")
        family = entry.entry_id.split(".", 1)[0]
        if entry.operation and family in OPERATION_FAMILIES:
            operations[family].add(entry.operation)
            if entry.operation not in OPERATION_FAMILIES[family][0]:
                errors.append(f"{entry.entry_id}: unknown operation")
        elif entry.operation:
            errors.append(f"{entry.entry_id}: unexpected operation")
        for field in (
            "qualified_name",
            "owner",
            "plan_builder",
            "apply_function",
            "epoch_owner",
        ):
            name = getattr(entry, field)
            if name in {"none", "caller_owned"}:
                continue
            if resolve(name) is None:
                errors.append(f"{entry.entry_id}: missing {field}: {name}")
            if field in {"qualified_name", "owner"}:
                covered.add(name)
        for source in entry.source:
            if resolve(source) is None:
                errors.append(f"{entry.entry_id}: missing source: {source}")

    # Shared dispatcher names cannot conceal a missing/new operation.
    for family, (expected, module, variable) in OPERATION_FAMILIES.items():
        actual_operations = set()
        for node in ast.walk(tree(module)):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == variable
                for target in node.targets
            ):
                for mapping in ast.walk(node.value):
                    if isinstance(mapping, ast.Dict):
                        actual_operations.update(
                            key.value
                            for key in mapping.keys
                            if isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                        )
        if operations[family] != actual_operations or actual_operations != set(
            expected
        ):
            errors.append(
                f"{family} operation inventory does not match the fixed dispatcher"
            )

    paths = {
        path
        for path in (root / "app/modules").rglob("*.py")
        if "candidate" in path.name
        or path.name.startswith("authorization_")
        and path.stem.endswith(("_owner", "_identity", "_audit"))
    }
    paths.update((root / "app/db").glob("authorization_*_owner.py"))
    paths.update((root / "app/modules/ai/service").glob("authorization_catalog_*.py"))
    paths.add(root / "app/modules/system/service/authorization_configuration_input.py")
    paths.add(root / "app/modules/system/service/authorization_ai_tool_adapter.py")
    paths.add(root / "app/modules/system/service/authorization_ai_file_adapter.py")
    paths.add(root / "app/modules/system/service/authorization_ai_user_input.py")
    paths.add(root / "app/modules/ai/service/authorization_chat_model.py")
    paths.add(root / "app/modules/ai/service/authorization_completion_projection.py")
    paths.add(root / "app/db/authorization_transfer_cache.py")
    paths.add(root / "app/db/authorization_runtime.py")
    paths.add(root / "app/modules/ai/agents/gateway/authorization_execution.py")
    paths.add(root / "app/modules/system/service/planned_user_field_policy_service.py")
    modules = {".".join(path.relative_to(root).with_suffix("").parts) for path in paths}
    modules.update(module_sources)
    for module in sorted(modules):
        current = tree(module)
        for node in current.body:
            if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                name = f"{module}.{node.name}"
                if name not in covered:
                    errors.append(f"unregistered public async candidate: {name}")
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                for method in node.body:
                    if isinstance(
                        method, ast.AsyncFunctionDef
                    ) and not method.name.startswith("_"):
                        name = f"{module}.{node.name}.{method.name}"
                        if name not in covered:
                            errors.append(
                                f"unregistered public async candidate: {name}"
                            )
    return sorted(set(errors))


def main():
    errors = check_candidates()
    if errors:
        for error in errors:
            print(error)
        return 1
    print(
        f"Authorization candidates: {len(CANDIDATES)} contracts checked; ingress evidence is verified separately"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
