"""Validate stable release metadata and extract its reviewed CHANGELOG notes."""

import argparse
import re
import tomllib
from pathlib import Path

STABLE_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)


def release_notes(changelog: str, tag: str) -> str:
    """Ignore fenced examples and reject ambiguous or unfinished release entries."""
    lines = changelog.splitlines(keepends=True)
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None and line.startswith("## "):
            headings.append((index, line.strip()))

    matches = [
        (position, index, heading)
        for position, (index, heading) in enumerate(headings)
        if re.match(rf"^## \[{re.escape(tag)}\](?:\s|$)", heading)
    ]
    if len(matches) != 1:
        raise ValueError(f"CHANGELOG must contain exactly one ## [{tag}] entry")
    position, index, heading = matches[0]
    if not re.fullmatch(
        rf"## \[{re.escape(tag)}\](?: (?:\(\d{{4}}-\d{{2}}-\d{{2}}\)|- \d{{4}}-\d{{2}}-\d{{2}}))?",
        heading,
    ):
        raise ValueError("Release heading must be finalized (no Unreleased marker)")
    if any(re.match(r"## \[v\d", item[1]) for item in headings[:position]):
        raise ValueError("Release must be the first version entry in CHANGELOG")
    end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
    notes = "".join(lines[index + 1 : end]).strip()
    if not notes:
        raise ValueError("Release notes must not be empty")
    return notes + "\n"


def prepare(root: Path, tag: str) -> str:
    if STABLE_TAG.fullmatch(tag) is None:
        raise ValueError("Tag must be a stable vMAJOR.MINOR.PATCH version")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    version = tag[1:]
    if project["version"] != version:
        raise ValueError("Tag and pyproject.toml version do not match")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [
        package
        for package in lock.get("package", [])
        if package["name"] == project["name"]
        and package.get("source") in ({"editable": "."}, {"virtual": "."})
    ]
    if len(packages) != 1 or packages[0]["version"] != version:
        raise ValueError("uv.lock must contain the matching local project version")
    return release_notes((root / "CHANGELOG.md").read_text(encoding="utf-8"), tag)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        notes = prepare(args.root, args.tag)
    except (OSError, KeyError, ValueError) as error:
        parser.exit(1, f"Release validation failed: {error}\n")
    args.notes_file.parent.mkdir(parents=True, exist_ok=True)
    args.notes_file.write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main()
