"""Freeze the first successful build and resume only its verified delivery."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from tools.ops import release_registry as registry
from tools.ops import sync_image_mirror as mirror
from tools.ops.release_tag import GitHub, manage_tag

REPOSITORIES = {"aihohu/hohu-admin", "aihohu/hohu-admin-web"}
STABLE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
RECORD_NAME = "component-build.json"
BUILD_JOB = "Build and Push Docker Image"
PLATFORMS = {"linux/amd64", "linux/arm64"}


def identity(repository, sha, tag, run_id, target):
    if (
        repository not in REPOSITORIES
        or not SHA.fullmatch(sha)
        or not STABLE.fullmatch(tag)
        or not re.fullmatch(r"[1-9][0-9]*", run_id)
    ):
        raise ValueError("Invalid original release identity")
    source = "ghcr.io/" + repository
    if target is not None:
        host, path = target.split("/", 1)
        mirror.validate_target(source, host, path)
    return {
        "schema_version": 1,
        "repository": repository,
        "source_sha": sha,
        "tag": tag,
        "run_id": run_id,
        "build_attempt": 1,
        "source_image": source,
        "mirror_image": target,
    }


def environment_identity():
    env = os.environ
    if env.get("RELEASE_EXCLUSIVE_WRITER_CONFIRMED") != "true":
        raise ValueError(
            "Exclusive writer policy must remain confirmed during recovery"
        )
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get(
        "GITHUB_REF"
    ) != "refs/heads/" + env.get("DEFAULT_BRANCH", ""):
        raise ValueError("Release requires the default-branch dispatch context")
    source = "ghcr.io/" + env["GITHUB_REPOSITORY"]
    target = None
    if env.get("ACR_MIRROR_ENABLED") == "true":
        target = mirror.validate_target(
            source, env.get("ACR_REGISTRY", ""), env.get("ACR_REPOSITORY", "")
        )
    return identity(
        env["GITHUB_REPOSITORY"],
        env["GITHUB_SHA"],
        env["RELEASE_TAG"],
        env["GITHUB_RUN_ID"],
        target,
    )


def guard(mode, attempt):
    if mode not in {"release", "test"} or not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise ValueError("Invalid build mode or attempt")
    if mode == "release" and attempt != "1":
        raise ValueError(
            "Formal build reruns are forbidden; only successful-build downstream recovery is supported"
        )


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def preflight(expected, *, exclusive_writer, probe=registry.probe_digest):
    if exclusive_writer != "true":
        raise ValueError(
            "Exclusive release writer policy must be verified before formal publication"
        )
    images = [expected["source_image"]]
    if expected["mirror_image"]:
        images.append(expected["mirror_image"])
    observations = {}
    for image in images:
        ref = image + ":" + expected["tag"][1:]
        if probe(image, expected["tag"][1:]) is not None:
            raise ValueError("Complete version already exists; refusing a new build")
        observations[ref] = "authenticated_manifest_unknown"
    return {
        "identity": expected,
        "exclusive_writer_confirmed": True,
        "targets": observations,
    }


def index_platforms(raw, digest):
    if not DIGEST.fullmatch(digest) or registry.digest(raw) != digest:
        raise ValueError("Build index digest mismatch")
    payload = registry.decode(raw)
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != 2
        or payload.get("mediaType")
        not in {
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        }
    ):
        raise ValueError("Build must have a multi-platform index")
    result = {}
    entries = payload.get("manifests")
    if not isinstance(entries, list):
        raise ValueError("Missing platform manifests")
    for entry in entries:
        platform = entry.get("platform", {})
        name = f"{platform.get('os')}/{platform.get('architecture')}"
        child = entry.get("digest", "")
        if (
            name not in PLATFORMS
            or name in result
            or not isinstance(child, str)
            or not DIGEST.fullmatch(child)
            or type(entry.get("size")) is not int
            or entry["size"] <= 0
            or entry.get("mediaType")
            not in {
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            }
        ):
            raise ValueError("Invalid, duplicate or unsupported platform manifest")
        result[name] = child
    if set(result) != PLATFORMS:
        raise ValueError("Incomplete platform set")
    return result


def freeze_build(expected, digest, precheck, *, inspect=mirror._inspect):
    wanted = preflight(expected, exclusive_writer="true", probe=lambda *_: None)
    if canonical(precheck) != canonical(wanted):
        raise ValueError("Build preflight evidence does not match original identity")
    source = expected["source_image"]
    raw = inspect(source + "@" + digest, 120)
    platforms = index_platforms(raw, digest)
    if registry.digest(inspect(source + ":" + expected["tag"][1:], 120)) != digest:
        raise ValueError("Complete source tag drifted after build")
    for child in platforms.values():
        if registry.digest(inspect(source + "@" + child, 120)) != child:
            raise ValueError("Child manifest digest mismatch")
    return {
        **expected,
        "index_digest": digest,
        "platforms": platforms,
        "preflight": precheck,
    }


def validate_record(raw, expected):
    if len(raw) > 1024 * 1024:
        raise ValueError("Build record too large")
    record = registry.decode(raw)
    if not isinstance(record, dict) or set(record) != set(expected) | {
        "index_digest",
        "platforms",
        "preflight",
    }:
        raise ValueError("Invalid build record fields")
    if any(
        type(record[k]) is not type(v) or record[k] != v for k, v in expected.items()
    ):
        raise ValueError("Build record belongs to another identity")
    if not isinstance(record["index_digest"], str) or not DIGEST.fullmatch(
        record["index_digest"]
    ):
        raise ValueError("Invalid build digest")
    platforms = record["platforms"]
    if (
        not isinstance(platforms, dict)
        or set(platforms) != PLATFORMS
        or not all(
            isinstance(d, str) and DIGEST.fullmatch(d) for d in platforms.values()
        )
    ):
        raise ValueError("Invalid build platform evidence")
    if canonical(record["preflight"]) != canonical(
        preflight(expected, exclusive_writer="true", probe=lambda *_: None)
    ):
        raise ValueError("Invalid original preflight evidence")
    return record


def verify_source(record, *, inspect=mirror._inspect):
    expected = {
        k: record[k]
        for k in identity(
            record["repository"],
            record["source_sha"],
            record["tag"],
            record["run_id"],
            record["mirror_image"],
        )
    }
    actual = freeze_build(
        expected, record["index_digest"], record["preflight"], inspect=inspect
    )
    if actual != record:
        raise ValueError("Live source differs from the original build record")


class DeliveryGitHub(GitHub):
    def __init__(self, repository, token):
        super().__init__(repository, token)
        self.repository = repository

    def download(self, path):
        # Actions archive endpoints negotiate JSON before redirecting to ZIP bytes;
        # Release asset endpoints instead require the octet-stream media type.
        accept = (
            "application/vnd.github+json"
            if path.startswith("actions/artifacts/")
            else "application/octet-stream"
        )
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{self.repository}/{path}",
                    "-H",
                    f"Accept: {accept}",
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            raise ValueError("GitHub evidence download failed") from None
        if len(result.stdout) > 16 * 1024 * 1024:
            raise ValueError("GitHub evidence download too large")
        return result.stdout

    def upload(self, tag, path):
        try:
            subprocess.run(
                ["gh", "release", "upload", tag, str(path), "--repo", self.repository],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            raise ValueError(
                "Release evidence upload outcome unknown; read back on retry"
            ) from None


def restore(api, expected):
    run_id = expected["run_id"]
    run = api.request("GET", f"actions/runs/{run_id}/attempts/1")
    if (
        str(run.get("id")) != run_id
        or run.get("run_attempt") != 1
        or run.get("head_sha") != expected["source_sha"]
        or run.get("event") != "workflow_dispatch"
        or run.get("path") != ".github/workflows/release.yml"
        or run.get("repository", {}).get("full_name") != expected["repository"]
    ):
        raise ValueError("Original workflow run identity mismatch")
    response = api.request("GET", f"actions/runs/{run_id}/attempts/1/jobs?per_page=100")
    jobs = response["jobs"]
    if response["total_count"] != len(jobs):
        raise ValueError("Incomplete original job inventory")
    builds = [j for j in jobs if j.get("name") == BUILD_JOB]
    if (
        len(builds) != 1
        or builds[0].get("status") != "completed"
        or builds[0].get("conclusion") != "success"
        or builds[0].get("head_sha") != expected["source_sha"]
    ):
        raise ValueError("Original first-attempt build did not succeed")
    response = api.request("GET", f"actions/runs/{run_id}/artifacts?per_page=100")
    artifacts = response["artifacts"]
    if response["total_count"] != len(artifacts):
        raise ValueError("Incomplete build artifact inventory")
    matches = [a for a in artifacts if a.get("name") == f"component-build-{run_id}-1"]
    if len(matches) != 1:
        raise ValueError("Original build artifact is missing or ambiguous")
    artifact = matches[0]
    origin = artifact.get("workflow_run", {})
    if (
        artifact.get("expired") is not False
        or str(origin.get("id")) != run_id
        or origin.get("head_sha") != expected["source_sha"]
        or not isinstance(artifact.get("digest"), str)
        or not DIGEST.fullmatch(artifact["digest"])
    ):
        raise ValueError("Original build artifact identity or retention invalid")
    start, created, end = (
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        for v in (
            builds[0]["started_at"],
            artifact["created_at"],
            builds[0]["completed_at"],
        )
    )
    if not start <= created <= end:
        raise ValueError("Artifact was not created during the successful first build")
    archive = api.download(f"actions/artifacts/{artifact['id']}/zip")
    if registry.digest(archive) != artifact["digest"]:
        raise ValueError("Original artifact archive digest mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        entries = bundle.infolist()
        if (
            len(entries) != 1
            or entries[0].filename != RECORD_NAME
            or entries[0].file_size > 1024 * 1024
        ):
            raise ValueError("Invalid build artifact archive contents")
        raw = bundle.read(entries[0])
    return raw, validate_record(raw, expected)


def delivery_evidence(
    record,
    raw,
    work_dir,
    *,
    inspect=mirror._inspect,
    probe=registry.probe_digest,
    pull=mirror._anonymous_pull,
):
    verify_source(record, inspect=inspect)
    work_dir.mkdir(parents=True, exist_ok=True)
    images = [record["source_image"]]
    if record["mirror_image"]:
        images.append(record["mirror_image"])
    for image in images:
        if probe(image, record["tag"][1:]) != record["index_digest"]:
            raise ValueError("Delivery target missing or conflicts with original build")
        if (
            index_platforms(
                inspect(image + ":" + record["tag"][1:], 120, anonymous=True),
                record["index_digest"],
            )
            != record["platforms"]
        ):
            raise ValueError("Public delivery index differs")
        pull(image, record["index_digest"], work_dir, 600)
    return {
        "schema_version": 1,
        "build_record_sha256": registry.digest(raw),
        "repository": record["repository"],
        "source_sha": record["source_sha"],
        "tag": record["tag"],
        "index_digest": record["index_digest"],
        "platforms": record["platforms"],
        "verified_images": images,
        "anonymous_pull": True,
    }


def preserve_asset(api, release, tag, path):
    assets = api.request("GET", f"releases/{release['id']}/assets?per_page=100")
    if not isinstance(assets, list) or len(assets) >= 100:
        raise ValueError("Release asset inventory incomplete")
    matches = [a for a in assets if a.get("name") == path.name]
    if len(matches) > 1:
        raise ValueError("Ambiguous release evidence asset")
    if not matches:
        api.upload(tag, path)
        # An upload acknowledgement is insufficient: verify stored bytes too.
        assets = api.request("GET", f"releases/{release['id']}/assets?per_page=100")
        matches = [a for a in assets if a.get("name") == path.name]
    if (
        len(matches) != 1
        or api.download(f"releases/assets/{matches[0]['id']}") != path.read_bytes()
    ):
        raise ValueError(
            "Release evidence differs; existing assets are never overwritten"
        )


def publish(api, expected, record_path, evidence_path, notes):
    notes = notes.rstrip() + (
        "\n\nThis announcement confirms component delivery only. "
        "It does not certify a Backend/Web product combination.\n"
    )
    manage_tag(
        api,
        "verify",
        tag=expected["tag"],
        sha=expected["source_sha"],
        repository=expected["repository"],
        run_id=expected["run_id"],
    )
    tag = expected["tag"]
    release = api.request("GET", f"releases/tags/{tag}", missing_ok=True)
    if release is None:
        release = api.request(
            "POST",
            "releases",
            {
                "tag_name": tag,
                "target_commitish": expected["source_sha"],
                "name": tag,
                "body": notes,
                "draft": True,
                "prerelease": False,
            },
        )
    if release.get("tag_name") != tag:
        raise ValueError("Release identity mismatch")
    preserve_asset(api, release, tag, record_path)
    preserve_asset(api, release, tag, evidence_path)
    if release.get("draft") is True:
        api.request(
            "PATCH", f"releases/{release['id']}", {"draft": False, "body": notes}
        )
    elif release.get("draft") is not False:
        raise ValueError("Release publication state unknown")
    published = api.request("GET", f"releases/tags/{tag}", missing_ok=True)
    if (
        not published
        or published.get("tag_name") != tag
        or published.get("draft") is not False
    ):
        raise ValueError("Release publication not confirmed by readback")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("guard", "preflight", "record", "restore", "publish")
    )
    parser.add_argument("--mode", choices=("release", "test"), default="release")
    parser.add_argument(
        "--preflight", type=Path, default=Path(".local/artifacts/build-preflight.json")
    )
    parser.add_argument(
        "--record", type=Path, default=Path(".local/artifacts/component-build.json")
    )
    parser.add_argument(
        "--work-dir", type=Path, default=Path(".local/tmp/release-delivery")
    )
    parser.add_argument(
        "--notes-file", type=Path, default=Path(".local/artifacts/release-notes.md")
    )
    args = parser.parse_args()
    try:
        if args.action == "guard":
            guard(args.mode, os.environ.get("GITHUB_RUN_ATTEMPT", ""))
            return
        expected = environment_identity()
        if args.action in {"preflight", "record"}:
            guard("release", os.environ.get("GITHUB_RUN_ATTEMPT", ""))
        if args.action == "preflight":
            write(
                args.preflight,
                preflight(
                    expected,
                    exclusive_writer=os.environ.get(
                        "RELEASE_EXCLUSIVE_WRITER_CONFIRMED", ""
                    ),
                ),
            )
        elif args.action == "record":
            record = freeze_build(
                expected,
                os.environ.get("IMAGE_DIGEST", ""),
                registry.decode(args.preflight.read_bytes()),
            )
            write(args.record, record)
        else:
            api = DeliveryGitHub(expected["repository"], os.environ.get("GH_TOKEN", ""))
            raw, record = restore(api, expected)
            verify_source(record)
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_bytes(raw)
            if args.action == "restore":
                with Path(os.environ["GITHUB_ENV"]).open(
                    "a", encoding="utf8"
                ) as output:
                    output.write(
                        f"IMAGE_DIGEST={record['index_digest']}\nIMAGE_TAGS={record['source_image']}:{record['tag'][1:]}\n"
                    )
            else:
                evidence = args.record.with_name("component-delivery.json")
                write(evidence, delivery_evidence(record, raw, args.work_dir))
                publish(
                    api,
                    expected,
                    args.record,
                    evidence,
                    args.notes_file.read_text(encoding="utf8"),
                )
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ):
        # No credential-bearing response, subprocess arguments or raw body in logs.
        parser.exit(
            1,
            "Release build evidence verification failed; preserve this version and inspect the original run.\n",
        )


if __name__ == "__main__":
    main()
