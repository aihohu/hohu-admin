"""Pure AST coverage gate for dormant authorization candidates; no DB imports."""

import ast
from pathlib import Path

from app.core.authorization_candidates import CANDIDATES, IO_PROFILES, SYSTEM_OPERATIONS

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

    seen, covered, operations = set(), set(), set()
    for entry in entries:
        if entry.entry_id in seen:
            errors.append(f"duplicate entry_id: {entry.entry_id}")
        seen.add(entry.entry_id)
        if entry.io_profile not in IO_PROFILES:
            errors.append(f"{entry.entry_id}: unknown io_profile")
        if entry.phase not in {"owner", "proof_writer", "plan", "read"}:
            errors.append(f"{entry.entry_id}: unknown phase")
        if entry.G_mode not in {"none", "S", "X"} or any(
            mode not in {"S", "X"} for mode in entry.B_modes
        ):
            errors.append(f"{entry.entry_id}: unknown barrier mode")
        if entry.production_connected is not False or entry.maintenance is not False:
            errors.append(
                f"{entry.entry_id}: unsupported production_connected/maintenance claim"
            )
        if entry.phase == "proof_writer" and not entry.proof_only:
            errors.append(f"{entry.entry_id}: proof writer must require proof")
        if entry.entry_id.startswith("system."):
            operations.add(entry.operation)
            if entry.operation not in SYSTEM_OPERATIONS:
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
    system = tree("app.modules.system.service.authorization_system_candidate")
    actual_operations = set()
    for node in ast.walk(system):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SYSTEM_CANDIDATE_ENTRIES"
            for target in node.targets
        ):
            for mapping in ast.walk(node.value):
                if isinstance(mapping, ast.Dict):
                    actual_operations.update(
                        key.value
                        for key in mapping.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
    if operations != actual_operations or actual_operations != set(SYSTEM_OPERATIONS):
        errors.append("System operation inventory does not match the fixed dispatcher")

    paths = {
        path
        for path in (root / "app/modules").rglob("*.py")
        if "candidate" in path.name
    }
    paths.update((root / "app/db").glob("authorization_*_owner.py"))
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
        f"Authorization candidates: {len(CANDIDATES)} entries checked; production wiring remains pending"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
