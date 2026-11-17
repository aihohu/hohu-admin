"""Bounded, hash-verified static preparation bundle; no filesystem fallback route."""

import hashlib
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from app.core.field_policy.documents import read_json

TYPES = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def bounded_file(path, budget):
    with path.open("rb") as source:
        content = source.read(budget + 1)
    if len(content) > budget:
        raise ValueError("Preparation file exceeds budget")
    return content


class PreparationAssets:
    def __init__(self, directory, expected_commit):
        root = Path(directory).resolve(strict=True)
        manifest_path = (root / "policy-preparation.json").resolve(strict=True)
        if not manifest_path.is_relative_to(root):
            raise ValueError("Invalid preparation manifest path")
        raw = bounded_file(manifest_path, 262144)
        manifest = read_json(raw)
        if (
            set(manifest) != {"protocol", "version", "sourceCommit", "files"}
            or manifest["protocol"] != "field-management/1"
            or manifest["version"] != "0.2.0"
            or not re.fullmatch(r"[0-9a-f]{40}", expected_commit or "")
            or manifest["sourceCommit"] != expected_commit
            or type(manifest["files"]) is not dict
            or not 1 <= len(manifest["files"]) <= 4096
            or "policy-prepare.html" not in manifest["files"]
        ):
            raise ValueError("Incompatible preparation Web bundle")
        self.files = {}
        total = 0
        for name, expected in manifest["files"].items():
            path = root / name
            if (
                not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", name)
                or ".." in name.split("/")
                or name.startswith("/")
                or not path.resolve(strict=True).is_relative_to(root)
                or path.suffix not in TYPES
                or path.stat().st_size > 8 * 1024 * 1024
            ):
                raise ValueError("Invalid preparation asset")
            content = bounded_file(path, 8 * 1024 * 1024)
            total += len(content)
            if (
                len(content) > 8 * 1024 * 1024
                or total > 64 * 1024 * 1024
                or hashlib.sha256(content).hexdigest() != expected
            ):
                raise ValueError("Changed or oversized preparation asset")
            # Freeze verified bytes. A later filesystem mutation cannot change served content.
            self.files[name] = (content, TYPES[path.suffix])
        self.source_commit = expected_commit
        self.digest = hashlib.sha256(raw).hexdigest()

    def read(self, name):
        return self.files[name]

    def mount(self, app):
        async def asset(request: Request):
            name = request.url.path.lstrip("/") or "policy-prepare.html"
            try:
                content, mime = self.read(name)
            except KeyError:
                return Response(status_code=404)
            return Response(
                content,
                media_type=mime,
                headers={
                    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
                },
            )

        app.add_api_route("/", asset, methods=["GET"])
        app.add_api_route("/{asset_path:path}", asset, methods=["GET"])
