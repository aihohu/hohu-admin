"""Validate the public documentation inventory and repository-local links.

Uses only the standard library; does not load the application or contact services.
Content accuracy and external links still require human review.
"""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "docs/public-docs.txt"
ENTRY_POINTS = (
    "README.md",
    "README.zh_CN.md",
    "AGENTS.md",
    "CLAUDE.md",
    "scripts/README.md",
    "alembic/README.md",
)
LOCAL_PREFIXES = (".local/", ".tmp/")


def prose(markdown: str) -> str:
    """Exclude fenced examples from link checks."""
    lines = []
    fence = None
    for line in markdown.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def local_targets(markdown: str) -> list[str]:
    """Read ordinary Markdown links, reference definitions and HTML URLs."""
    text = prose(markdown)
    targets = re.findall(r"\]\(\s*(<[^>]+>|[^\s)]+)", text)
    targets += re.findall(r"^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", text, re.M)
    targets += re.findall(r"(?:href|src)=[\"']([^\"']+)[\"']", text)
    return [target.strip("<>") for target in targets]


def validate(root: Path, tracked: set[str]) -> list[str]:
    errors = []
    manifest = root / MANIFEST
    if not manifest.is_file():
        return [f"Missing public documentation inventory: {MANIFEST}"]
    entries = [
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(entries) != len(set(entries)):
        errors.append(f"{MANIFEST}: duplicate entries")
    approved = set(entries)
    actual = {
        path.relative_to(root).as_posix()
        for path in (root / "docs").rglob("*")
        if path.is_file()
    }
    for name in sorted(actual - approved):
        errors.append(f"Unreviewed public document: {name}")
    for name in sorted(approved - actual):
        errors.append(f"Missing public document: {name}")
    for name in sorted(tracked):
        if name.startswith(LOCAL_PREFIXES) and (root / name).exists():
            errors.append(f"Local working material is tracked: {name}")

    for name in sorted(approved | set(ENTRY_POINTS)):
        source = root / name
        if not source.resolve().is_relative_to(root.resolve()):
            errors.append(f"Inventory path escapes repository: {name}")
            continue
        if source.is_symlink():
            errors.append(f"Public document must not be a symlink: {name}")
            continue
        if source.suffix != ".md" or not source.is_file():
            continue
        for target in local_targets(source.read_text(encoding="utf-8")):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            resolved = (source.parent / unquote(url.path)).resolve()
            if not resolved.is_relative_to(root.resolve()):
                errors.append(f"{name}: link leaves standalone repository: {target}")
            elif not resolved.exists():
                errors.append(f"{name}: missing link target: {target}")
            elif (
                resolved.relative_to(root.resolve())
                .as_posix()
                .startswith(LOCAL_PREFIXES)
            ):
                errors.append(f"{name}: public document links local material: {target}")
    return errors


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    tracked = set(result.stdout.decode("utf-8").split("\0")) - {""}
    errors = validate(ROOT, tracked)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("Public documentation inventory and repository-local link paths passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
