"""Validate one product manifest; offline validity is not release qualification."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker, validators

from tools.ops import release_registry as registry
from tools.ops import sync_image_mirror as mirror

SCHEMA = Path(__file__).with_name("schemas") / "product-release-v1.schema.json"
REPOS = {"backend": "aihohu/hohu-admin", "web": "aihohu/hohu-admin-web"}
DOCS = "aihohu/hohu-admin-docs"
OFFICIAL = {*REPOS.values(), DOCS, "aihohu/hohu-cli"}
PAIR_KINDS = {"pair_install", "upgrade", "recovery", "ai_write_consistency"}
LIMIT = 1024 * 1024
FORMATS = FormatChecker()


def require(condition, message):
    if not condition:
        raise ValueError(message)


@FORMATS.checks("https-url", raises=ValueError)
def https_url(value):
    if not isinstance(value, str):
        return True  # Type validation belongs to the schema.
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not any(c.isspace() or ord(c) < 32 for c in value)
        and "\\" not in value
    )


@FORMATS.checks("utc-rfc3339", raises=ValueError)
def utc_date(value):
    if not isinstance(value, str):
        return True
    return bool(
        re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z",
            value,
        )
        and datetime.fromisoformat(value)
    )


def decode(raw):
    require(isinstance(raw, bytes) and len(raw) <= LIMIT, "Invalid manifest size")
    require(not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM is not supported")

    def finite_float(text):
        value = float(text)
        require(math.isfinite(value), "JSON number exceeds the finite range")
        return value

    try:
        return json.loads(
            raw.decode("utf8"),
            object_pairs_hook=registry.unique_object,
            parse_float=finite_float,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Non-finite JSON number")
            ),
        )
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("Invalid UTF-8 JSON") from None


def source_url(url):
    """Map only official, permanent report locations to fixed API operations."""
    require(https_url(url), "Invalid source URL")
    parsed = urlsplit(url)
    require(
        parsed.netloc == "github.com" and not parsed.query and not parsed.fragment,
        "Unapproved source origin",
    )
    require(not re.search(r"[^a-zA-Z0-9/_.-]", parsed.path), "Invalid source path")
    parts = parsed.path.strip("/").split("/")
    require(all(p and p not in {".", ".."} for p in parts), "Invalid source path")
    require(parsed.path == "/" + "/".join(parts), "Source URL is not canonical")
    repository = "/".join(parts[:2])
    require(repository in OFFICIAL, "Unapproved source repository")
    if (
        len(parts) >= 5
        and parts[2] == "blob"
        and re.fullmatch(r"[0-9a-f]{40}", parts[3])
    ):
        return repository, "content", parts[3], "/".join(parts[4:])
    if (
        len(parts) == 6
        and parts[2:4] == ["releases", "download"]
        and re.fullmatch(
            r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", parts[4]
        )
    ):
        return repository, "asset", parts[4], parts[5]
    raise ValueError("Source must be a pinned blob or stable Release asset")


def primary(component):
    images = [image for image in component["images"] if image["role"] == "primary"]
    require(len(images) == 1, "Exactly one primary image is required")
    return images[0]


def platforms(image):
    result = {
        p["platform"]: (p["manifest_digest"], p["qualification"])
        for p in image["platforms"]
    }
    require(len(result) == len(image["platforms"]), "Duplicate platform")
    return result


def version(value):
    return tuple(int(part) for part in value.split("."))


def validate(raw):
    value = decode(raw)
    schema = json.loads(SCHEMA.read_text(encoding="utf8"))
    strict = validators.extend(
        Draft202012Validator,
        type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
            "integer", lambda _, item: type(item) is int
        ),
    )
    strict.check_schema(schema)
    error = next(strict(schema, format_checker=FORMATS).iter_errors(value), None)
    # Never include arbitrary values (which could contain credentials) in diagnostics.
    require(error is None, "Manifest does not satisfy the v1 schema")
    product = value["product_version"]
    support = {}
    for key, component in value["components"].items():
        require(
            component["version"] == product and component["tag"] == "v" + product,
            "Component version/tag mismatch",
        )
        require(component["repository"] == REPOS[key], "Component repository mismatch")
        require(
            re.fullmatch(
                rf"https://github.com/{REPOS[key]}/actions/runs/[1-9][0-9]*",
                component["build_run_url"],
            ),
            "Invalid build run URL",
        )
        p = primary(component)
        require(p["repository"] == "ghcr.io/" + REPOS[key], "Unapproved primary image")
        mapping = platforms(p)
        require(
            len({i["repository"] for i in component["images"]})
            == len(component["images"]),
            "Duplicate image repository",
        )
        for image in component["images"]:
            require(
                image["index_digest"] == p["index_digest"]
                and platforms(image) == mapping,
                "Mirror differs from primary",
            )
            if image["role"] == "mirror":
                host, path = image["repository"].split("/", 1)
                mirror.validate_target(p["repository"], host, path)
        support[key] = {
            name
            for name, (_, qualification) in mapping.items()
            if qualification == "verified"
        }
    require(
        support["backend"] and support["backend"] == support["web"],
        "Supported platform sets must be equal and nonempty",
    )
    ci = set()
    pair = set()
    for evidence in value["evidence"]:
        for key, component in value["components"].items():
            require(
                evidence[key + "_commit"] == component["commit"],
                "Evidence commit mismatch",
            )
        source_url(evidence["url"])
        if evidence["kind"] == "component_ci":
            key = evidence["subject"]
            require(key in REPOS, "Component CI must name a component")
            other = "web" if key == "backend" else "backend"
            require(
                evidence[other + "_digest"] is None,
                "Component CI has other-component digest",
            )
            d = evidence[key + "_digest"]
            p = primary(value["components"][key])
            require(
                (d is None and evidence["platform"] is None)
                or (d == p["index_digest"] and evidence["platform"] in platforms(p)),
                "Invalid component CI digest/platform",
            )
            ci.add(key)
        else:
            require(
                evidence["subject"] == "pair"
                and evidence["platform"] in support["backend"],
                "Pair evidence must name a supported platform",
            )
            for key, component in value["components"].items():
                require(
                    evidence[key + "_digest"] == primary(component)["index_digest"],
                    "Pair evidence digest mismatch",
                )
            pair.add((evidence["kind"], evidence["platform"]))
    require(ci == set(REPOS), "Missing component CI evidence")
    require(
        pair == {(kind, p) for p in support["backend"] for kind in PAIR_KINDS},
        "Missing supported-platform evidence",
    )
    docs = value["docs"]
    require(
        docs["series"] == ".".join(product.split(".")[:2])
        and docs["repository"] == DOCS,
        "Docs series/repository mismatch",
    )
    repo, kind, sha, _ = source_url(docs["snapshot_url"])
    require(
        (repo, kind, sha) == (DOCS, "content", docs["commit"]),
        "Docs snapshot must pin the declared SHA",
    )
    for url in [
        docs["url"],
        value["upgrade"]["procedure_url"],
        value["upgrade"]["recovery_url"],
        value["cli"]["procedure_url"],
    ]:
        source_url(url)
    cli = value["cli"]
    require(
        cli["minimum_version"] in cli["tested_versions"]
        and all(
            version(v) >= version(cli["minimum_version"])
            for v in cli["tested_versions"]
        ),
        "CLI minimum must be tested",
    )
    for client in value["clients"].values():
        require(
            bool(client["verified"]) == (client["status"] == "verified"),
            "Client status/proof mismatch",
        )
        for proof in client["verified"]:
            require(
                product in proof["server_versions"],
                "Client proof does not cover this product",
            )
            source_url(proof["proof_url"])
    if product == "0.1.6":
        require(
            value["upgrade"]["from_versions"] == ["0.1.5"],
            "Unqualified upgrade source for 0.1.6",
        )
        require(
            version(cli["minimum_version"]) >= (0, 1, 16),
            "CLI is below the 0.1.6 candidate minimum",
        )
        require(
            all(
                c == {"status": "not_released", "verified": []}
                for c in value["clients"].values()
            ),
            "No released clients are qualified for 0.1.6",
        )
    return value


def check_file(path, *, sources=None, work_dir=None):
    value = validate(path.read_bytes())
    result = {
        "schema_version": 1,
        "offline": "passed",
        "online": "not_run",
        "product_version": value["product_version"],
    }
    if sources is not None:
        sources.verify(value, work_dir)
        result["online"] = "passed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--allow-mirror", action="append", default=[])
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    try:
        sources = None
        if args.online:
            require(
                args.work_dir is not None, "Online verification requires --work-dir"
            )
            sources = make_sources(args.allow_mirror)
        result = check_file(args.manifest, sources=sources, work_dir=args.work_dir)
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(
            1, "Product manifest verification failed; no publication is authorized.\n"
        )


def make_sources(mirrors):
    # Lazy import keeps the offline command free of online dependencies/calls.
    from tools.ops.product_release_sources import Sources  # noqa: PLC0415

    return Sources(allowed_mirrors=mirrors)


if __name__ == "__main__":
    main()
