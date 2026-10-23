"""Read only approved release sources; no commands are read from a manifest."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from datetime import UTC, datetime

from tools.ops import check_product_release as check
from tools.ops import release_build as build
from tools.ops import release_registry as registry
from tools.ops import sync_image_mirror as mirror
from tools.ops.prepare_release import release_notes

require = check.require
MAX_DOWNLOAD = 16 * 1024 * 1024


def integer_id(value):
    require(type(value) is int and value > 0, "Invalid GitHub object identity")
    return str(value)


class Sources:
    def __init__(self, *, allowed_mirrors=()):
        self.allowed_mirrors = set(allowed_mirrors)
        for image in self.allowed_mirrors:
            host, path = image.split("/", 1)
            mirror.validate_target(
                "ghcr.io/aihohu/" + path.rsplit("/", 1)[-1], host, path
            )

    def api_bytes(self, repo, path, *, accept="application/vnd.github+json"):
        require(
            repo in check.OFFICIAL
            and not path.startswith(("/", "-"))
            and ".." not in path,
            "Unapproved GitHub request",
        )
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    "GET",
                    f"repos/{repo}/{path}",
                    "-H",
                    f"Accept: {accept}",
                ],
                stdin=subprocess.DEVNULL,
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            raise ValueError("GitHub request failed; source state is unknown") from None
        require(len(result.stdout) <= MAX_DOWNLOAD, "GitHub response is too large")
        return result.stdout

    def request(self, repo, path):
        return check.decode(self.api_bytes(repo, path))

    def content(self, repo, sha, path):
        require(re.fullmatch(r"[0-9a-f]{40}", sha), "Source commit must be pinned")
        require(
            re.fullmatch(r"[a-zA-Z0-9_./-]+", path)
            and all(p not in {"", ".", ".."} for p in path.split("/")),
            "Invalid source file path",
        )
        return self.api_bytes(
            repo, f"contents/{path}?ref={sha}", accept="application/vnd.github.raw+json"
        )

    def release(self, repo, tag):
        release = self.request(repo, "releases/tags/" + tag)
        require(
            release.get("tag_name") == tag
            and release.get("draft") is False
            and release.get("prerelease") is False,
            "Release must be public and stable",
        )
        integer_id(release.get("id"))
        return release

    def assets(self, repo, release):
        assets = self.request(
            repo, f"releases/{integer_id(release['id'])}/assets?per_page=100"
        )
        require(
            isinstance(assets, list) and len(assets) < 100,
            "Release asset inventory incomplete",
        )
        for asset in assets:
            require(
                isinstance(asset, dict) and isinstance(asset.get("name"), str),
                "Malformed asset inventory",
            )
            integer_id(asset.get("id"))
        return assets

    def download_asset(self, repo, asset):
        require(
            asset.get("state") == "uploaded"
            and type(asset.get("size")) is int
            and 0 < asset["size"] <= MAX_DOWNLOAD,
            "Asset is not a complete bounded upload",
        )
        raw = self.api_bytes(
            repo,
            "releases/assets/" + integer_id(asset["id"]),
            accept="application/octet-stream",
        )
        require(len(raw) == asset["size"], "Asset download size mismatch")
        if asset.get("digest") is not None:
            require(
                registry.digest(raw) == asset["digest"],
                "Asset download digest mismatch",
            )
        return raw

    def asset(self, repo, release, name):
        matches = [a for a in self.assets(repo, release) if a["name"] == name]
        require(len(matches) == 1, "Release asset is missing or ambiguous")
        return self.download_asset(repo, matches[0])

    def fetch(self, url):
        repo, kind, ref, path = check.source_url(url)
        if kind == "content":
            return self.content(repo, ref, path)
        return self.asset(repo, self.release(repo, ref), path)

    def inspect(self, reference):
        try:
            return mirror._inspect(reference, 120, anonymous=True)
        except (subprocess.SubprocessError, OSError):
            raise ValueError("Public image inspection failed") from None

    def pull(self, image, digest, work_dir):
        try:
            mirror._anonymous_pull(image, digest, work_dir, 600)
        except (subprocess.SubprocessError, OSError):
            raise ValueError("Public image download failed") from None

    def verify(self, doc, work_dir):
        doc = check.validate(json.dumps(doc).encode())
        require(work_dir is not None, "Online verification requires a work directory")
        # All destinations are vetted before the first request or pull.
        for component in doc["components"].values():
            require(
                all(
                    i["role"] != "mirror" or i["repository"] in self.allowed_mirrors
                    for i in component["images"]
                ),
                "Mirror has not been explicitly approved",
            )
        published = datetime.fromisoformat(doc["published_at"])
        require(
            published <= datetime.now(UTC), "Product completion time is in the future"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            for key, component in doc["components"].items():
                self.verify_component(key, component, published, work_dir)
            self.verify_migrations(
                doc["components"]["backend"]["commit"],
                doc["upgrade"]["database_revision"],
            )
            urls = {
                doc["docs"]["url"],
                doc["docs"]["snapshot_url"],
                doc["cli"]["procedure_url"],
                doc["upgrade"]["procedure_url"],
                doc["upgrade"]["recovery_url"],
            }
            for url in urls:
                require(bool(self.fetch(url)), "Empty documentation")
            for evidence in doc["evidence"]:
                report = self.fetch(evidence["url"])
                require(
                    bool(report) and registry.digest(report) == evidence["sha256"],
                    "Evidence bytes do not match the declared digest",
                )
            for client in doc["clients"].values():
                for proof in client["verified"]:
                    require(
                        registry.digest(self.fetch(proof["proof_url"]))
                        == proof["proof_sha256"],
                        "Client proof digest mismatch",
                    )
        except (KeyError, TypeError, IndexError, UnicodeError, SyntaxError):
            raise ValueError("Release source response is malformed") from None

    def verify_component(self, key, component, published, work_dir):
        repo, sha, tag = component["repository"], component["commit"], component["tag"]
        run_id = component["build_run_url"].rsplit("/", 1)[1]
        ref = self.request(repo, f"git/ref/tags/{tag}")
        require(
            ref["object"]["type"] == "tag"
            and re.fullmatch(r"[0-9a-f]{40}", ref["object"]["sha"]),
            "Release requires its owned annotated tag",
        )
        annotation = self.request(repo, "git/tags/" + ref["object"]["sha"])
        message = f"Release {tag}\n\nGitHub Actions run: {component['build_run_url']}"
        require(
            annotation["tag"] == tag
            and annotation["object"].get("type") == "commit"
            and annotation["object"].get("sha") == sha,
            "Release tag source mismatch",
        )
        require(
            annotation["message"].strip() == message,
            "Release tag does not belong to the declared build run",
        )
        run = self.request(repo, f"actions/runs/{run_id}/attempts/1")
        require(
            str(run["id"]) == run_id
            and run["run_attempt"] == 1
            and run["head_sha"] == sha
            and run["head_branch"] == "main"
            and run["event"] == "workflow_dispatch"
            and run["path"] == ".github/workflows/release.yml"
            and run["repository"]["full_name"] == repo,
            "Original release run identity mismatch",
        )
        response = self.request(
            repo, f"actions/runs/{run_id}/attempts/1/jobs?per_page=100"
        )
        jobs = response["jobs"]
        require(
            response["total_count"] == len(jobs), "Original job inventory incomplete"
        )
        needed = [
            build.BUILD_JOB,
            "Hosted release qualification (release profile)"
            if key == "backend"
            else "Verify release source / Check and build frontend",
        ]
        for name in needed:
            matches = [job for job in jobs if job["name"] == name]
            require(
                len(matches) == 1, "Required original release job missing or ambiguous"
            )
            job = matches[0]
            require(
                job["head_sha"] == sha
                and job["status"] == "completed"
                and job["conclusion"] == "success"
                and datetime.fromisoformat(job["completed_at"]) <= published,
                "Required original release job did not qualify before publication",
            )
        release = self.release(repo, tag)
        record_raw = self.asset(repo, release, "component-build.json")
        record = check.decode(record_raw)
        target = record.get("mirror_image")
        require(
            target is None or target in self.allowed_mirrors,
            "Build mirror is not approved",
        )
        expected = build.identity(repo, sha, tag, run_id, target)
        record = build.validate_record(record_raw, expected)
        primary = check.primary(component)
        mapping = {
            name: digest for name, (digest, _) in check.platforms(primary).items()
        }
        images = [i["repository"] for i in component["images"]]
        require(
            record["index_digest"] == primary["index_digest"]
            and record["platforms"] == mapping
            and set(images)
            == {image for image in (record["source_image"], target) if image},
            "Manifest differs from original build",
        )
        expected_delivery = {
            "schema_version": 1,
            "build_record_sha256": registry.digest(record_raw),
            "repository": repo,
            "source_sha": sha,
            "tag": tag,
            "index_digest": primary["index_digest"],
            "platforms": mapping,
            "verified_images": [record["source_image"]] + ([target] if target else []),
            "anonymous_pull": True,
        }
        delivery = check.decode(self.asset(repo, release, "component-delivery.json"))
        require(
            build.canonical(delivery) == build.canonical(expected_delivery),
            "Original component delivery evidence mismatch",
        )
        self.verify_metadata(key, component, release)
        for image in component["images"]:
            repository, digest = image["repository"], image["index_digest"]
            require(
                registry.digest(self.inspect(repository + ":" + component["version"]))
                == digest,
                "Complete image tag drift",
            )
            require(
                build.index_platforms(self.inspect(repository + "@" + digest), digest)
                == mapping,
                "Image index/platform mapping mismatch",
            )
            for child in mapping.values():
                require(
                    registry.digest(self.inspect(repository + "@" + child)) == child,
                    "Platform manifest digest mismatch",
                )
            self.pull(repository, digest, work_dir)
            require(
                registry.digest(self.inspect(repository + ":" + component["version"]))
                == digest,
                "Complete image tag drifted during download",
            )
        readback = self.request(repo, f"git/ref/tags/{tag}")
        require(
            readback["object"]["type"] == "tag"
            and readback["object"]["sha"] == ref["object"]["sha"],
            "Release tag changed during verification",
        )

    def verify_metadata(self, key, component, release):
        repo, sha, version = (
            component["repository"],
            component["commit"],
            component["version"],
        )
        notes = release_notes(
            self.content(repo, sha, "CHANGELOG.md").decode("utf8"), component["tag"]
        )
        require(notes.strip() in release["body"], "Release notes differ from source")
        if key == "backend":
            project = tomllib.loads(
                self.content(repo, sha, "pyproject.toml").decode("utf8")
            )["project"]
            lock = tomllib.loads(self.content(repo, sha, "uv.lock").decode("utf8"))
            packages = [
                p
                for p in lock["package"]
                if p["name"] == project["name"]
                and p.get("source") in ({"editable": "."}, {"virtual": "."})
            ]
            require(
                project["version"] == version
                and len(packages) == 1
                and packages[0]["version"] == version,
                "Backend metadata/lock version mismatch",
            )
        else:
            package = check.decode(self.content(repo, sha, "package.json"))
            require(package["version"] == version, "Web metadata version mismatch")
            lock = self.content(repo, sha, "pnpm-lock.yaml").decode("utf8")
            # pnpm's importer does not store the application's own version.
            require(
                re.search(r"(?m)^lockfileVersion:", lock)
                and re.search(r"(?m)^importers:\s*\n\s+\.:", lock),
                "Web lockfile has no root importer",
            )

    def verify_migrations(self, sha, expected):
        repo = check.REPOS["backend"]
        tree = self.request(repo, f"git/trees/{sha}?recursive=1")
        require(tree.get("truncated") is False, "Migration source tree is incomplete")
        paths = [
            item["path"]
            for item in tree["tree"]
            if item["type"] == "blob"
            and re.fullmatch(r"alembic/versions/[^/]+\.py", item["path"])
            and not item["path"].endswith("/__init__.py")
        ]
        require(bool(paths), "Migration chain is missing")
        revisions = {}
        for path in paths:
            module = ast.parse(self.content(repo, sha, path).decode("utf8"))
            values = {}
            for node in module.body:
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                    if isinstance(node, ast.AnnAssign)
                    else []
                )
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "revision",
                        "down_revision",
                    }:
                        require(target.id not in values, "Duplicate migration metadata")
                        values[target.id] = ast.literal_eval(node.value)
            revision, parent = values["revision"], values["down_revision"]
            require(
                isinstance(revision, str) and revision and revision not in revisions,
                "Duplicate or invalid migration revision",
            )
            parents = (
                (parent,)
                if isinstance(parent, str)
                else parent
                if isinstance(parent, tuple)
                else ()
                if parent is None
                else None
            )
            require(
                parents is not None and all(isinstance(p, str) and p for p in parents),
                "Invalid migration parent",
            )
            revisions[revision] = parents
        parents = {p for ps in revisions.values() for p in ps}
        require(
            parents <= set(revisions) and set(revisions) - parents == {expected},
            "Migration target is not the unique real head",
        )
        visited = set()
        visiting = set()

        def walk(revision):
            require(revision not in visiting, "Migration cycle")
            if revision in visited:
                return
            visiting.add(revision)
            for parent in revisions[revision]:
                walk(parent)
            visiting.remove(revision)
            visited.add(revision)

        walk(expected)
        require(
            visited == set(revisions), "Migration chain contains disconnected revisions"
        )

    def upload(self, repo, tag, path):
        require(
            repo == check.REPOS["backend"] and path.name == "release.json",
            "Product manifest upload destination mismatch",
        )
        try:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "upload",
                    tag,
                    str(path),
                    "--repo",
                    "github.com/" + repo,
                ],
                stdin=subprocess.DEVNULL,
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            raise ValueError(
                "Upload acknowledgement unknown; readback is required"
            ) from None
