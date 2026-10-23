"""Upload a verified product manifest once, then confirm its original bytes."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from tools.ops import check_product_release as check
from tools.ops import release_registry as registry


def find_asset(sources, repo, release):
    assets = sources.assets(repo, release)
    check.require(
        isinstance(assets, list) and len(assets) < 100, "Asset inventory is incomplete"
    )
    matches = [a for a in assets if a["name"] == "release.json"]
    check.require(len(matches) <= 1, "Ambiguous product manifest asset")
    return matches[0] if matches else None


def upload_manifest(path, sources, work_dir):
    check.require(
        path.name == "release.json", "Only the final release.json may be uploaded"
    )
    raw = path.read_bytes()
    doc = check.validate(raw)
    sources.verify(doc, work_dir)
    check.require(path.read_bytes() == raw, "Manifest changed during verification")
    repo = check.REPOS["backend"]
    tag = "v" + doc["product_version"]
    release = sources.release(repo, tag)
    check.require(
        release.get("tag_name") == tag
        and release.get("draft") is False
        and release.get("prerelease") is False,
        "Product requires an existing public component Release",
    )
    existing = find_asset(sources, repo, release)
    if existing is None:
        # Never let an edit to the input path change the verified upload bytes.
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="product-upload-", dir=work_dir
        ) as directory:
            snapshot = Path(directory) / "release.json"
            snapshot.write_bytes(raw)
            try:
                sources.upload(repo, tag, snapshot)
            except (ValueError, OSError):
                # A failed acknowledgement cannot tell whether the write committed.
                # Exactly one write per invocation; resolve only by reading back.
                pass
        existing = find_asset(sources, repo, release)
    check.require(
        existing is not None,
        "Upload is not confirmed; preserve the original bytes before retrying",
    )
    check.require(
        sources.download_asset(repo, existing) == raw,
        "Existing manifest differs; replacement and deletion are forbidden",
    )
    return {
        "offline": "passed",
        "online": "passed",
        "upload": "confirmed",
        "sha256": registry.digest(raw),
        "url": f"https://github.com/{repo}/releases/download/{tag}/release.json",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--allow-mirror", action="append", default=[])
    args = parser.parse_args()
    try:
        result = upload_manifest(
            args.manifest, check.make_sources(args.allow_mirror), args.work_dir
        )
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(
            1,
            "Product manifest upload not confirmed; retain the original file, inspect readback, and never overwrite assets.\n",
        )


if __name__ == "__main__":
    main()
