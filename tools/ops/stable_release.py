"""Select fixed stable sources using trusted controller code and read-only APIs."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tools.ops.prepare_release import STABLE_TAG, release_notes

BASELINES = {
    "aihohu/hohu-admin": "71966784b4eb35d0fde9d8c62657137fdc562879",
    "aihohu/hohu-admin-web": "72d2a46d84704517ac7257e3a695fe9f66253f1c",
}
BASELINE_TAG = "v0.1.6"
SHA = re.compile(r"[0-9a-f]{40}")
FIELDS = {
    "channel",
    "controller_sha",
    "source_ref",
    "baseline_tag",
    "baseline_sha",
    "peer_repository",
    "peer_sha",
}
LIMIT = 2 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def version(tag, *, baseline=False):
    match = STABLE_TAG.fullmatch(tag) if isinstance(tag, str) else None
    require(match is not None, "Expected a stable version tag")
    value = tuple(map(int, match.groups()))
    require(value[:2] == (0, 1), "Only the supported 0.1 stable series is allowed")
    require(
        value[2] > 6 or (baseline and value[2] == 6), "A new stable patch is required"
    )
    return value


def controller(repository, controller_sha, default_branch, selected_ref, event):
    require(repository in BASELINES, "Unsupported official repository")
    require(valid_sha(controller_sha), "Controller must be a full commit SHA")
    require(
        default_branch == "main"
        and selected_ref == "refs/heads/main"
        and event == "workflow_dispatch",
        "Only the default-branch dispatch may select release sources",
    )


def validate_provenance(value, repository, tag):
    version(tag)
    require(repository in BASELINES, "Unsupported official repository")
    require(
        isinstance(value, dict) and set(value) == FIELDS,
        "Invalid stable provenance fields",
    )
    require(
        all(isinstance(v, str) for v in value.values()), "Invalid provenance field type"
    )
    require(value["channel"] == "stable-0.1", "Unsupported provenance channel")
    require(
        valid_sha(value["controller_sha"]) and valid_sha(value["peer_sha"]),
        "Invalid pinned provenance SHA",
    )
    require(
        value["source_ref"] == "refs/heads/release/" + tag[1:],
        "Stable ref must match the patch version",
    )
    require(
        value["baseline_tag"] == BASELINE_TAG
        and value["baseline_sha"] == BASELINES[repository],
        "Stable baseline differs from the approved release",
    )
    require(
        value["peer_repository"] in BASELINES
        and value["peer_repository"] != repository,
        "Invalid counterpart repository",
    )
    return dict(value)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        raise ValueError("GitHub API redirects are not accepted")


class Reader:
    def __init__(self, token):
        require(bool(token), "GH_TOKEN is required for release source verification")
        self.token = token
        self.deadline = time.monotonic() + 240
        self.calls = 0
        self.opener = build_opener(NoRedirect())

    def request(self, repository, path):
        require(repository in BASELINES, "Unsupported source repository")
        require(
            isinstance(path, str)
            and re.fullmatch(r"[a-zA-Z0-9_./?=%-]+", path)
            and not path.startswith("/")
            and ".." not in path.replace("...", ""),
            "Invalid source request path",
        )
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        require(
            self.calls <= 24 and remaining > 0,
            "Source verification IO budget exhausted",
        )
        request = Request(
            f"https://api.github.com/repos/{repository}/{path}",
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "hohu-stable-release",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=min(30, remaining)) as response:
                raw = response.read(LIMIT + 1)
            require(len(raw) <= LIMIT, "Source response exceeds the size budget")
            value = json.loads(raw)
            require(isinstance(value, dict), "Malformed source response")
            return value
        except (HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
            raise ValueError(
                "GitHub source query failed; release state is unknown"
            ) from None


def content(request, repository, sha, path):
    value = request(repository, f"contents/{path}?ref={sha}")
    require(
        value.get("type") == "file"
        and value.get("encoding") == "base64"
        and isinstance(value.get("content"), str),
        "Source metadata must be a bounded regular file",
    )
    try:
        raw = base64.b64decode("".join(value["content"].splitlines()), validate=True)
    except (KeyError, TypeError, binascii.Error):
        raise ValueError("Malformed source metadata") from None
    require(
        type(value.get("size")) is int
        and len(raw) == value["size"]
        and len(raw) <= LIMIT // 2,
        "Source metadata size mismatch",
    )
    return raw.decode("utf-8")


def verify_history(request, repository, sha):
    require(repository in BASELINES and valid_sha(sha), "Invalid stable source")
    release = request(repository, "releases/tags/" + BASELINE_TAG)
    require(
        release.get("tag_name") == BASELINE_TAG
        and release.get("draft") is False
        and release.get("prerelease") is False,
        "Baseline must remain a published stable release",
    )
    ref = request(repository, "git/ref/tags/" + BASELINE_TAG)
    obj = ref.get("object", {})
    require(
        obj.get("type") == "tag" and valid_sha(obj.get("sha")),
        "Baseline needs its annotated release tag",
    )
    annotation = request(repository, "git/tags/" + obj["sha"])
    require(
        annotation.get("tag") == BASELINE_TAG
        and annotation.get("object", {}).get("type") == "commit"
        and annotation["object"].get("sha") == BASELINES[repository],
        "Published baseline tag has changed",
    )
    comparison = request(repository, f"compare/{BASELINES[repository]}...{sha}")
    require(
        comparison.get("status") in {"ahead", "identical"}
        and comparison.get("merge_base_commit", {}).get("sha") == BASELINES[repository],
        "Stable source is not descended from the published baseline",
    )


def verify_metadata(request, repository, sha, tag):
    expected = tag[1:]
    if repository.endswith("-web"):
        package = json.loads(content(request, repository, sha, "package.json"))
        require(
            package.get("version") == expected, "Counterpart package version mismatch"
        )
    else:
        project = tomllib.loads(content(request, repository, sha, "pyproject.toml"))[
            "project"
        ]
        require(project.get("version") == expected, "Backend package version mismatch")
        lock = tomllib.loads(content(request, repository, sha, "uv.lock"))
        roots = [
            item
            for item in lock.get("package", [])
            if item.get("name") == project["name"]
            and item.get("source") in ({"editable": "."}, {"virtual": "."})
        ]
        require(
            len(roots) == 1 and roots[0].get("version") == expected,
            "Backend lock version mismatch",
        )
    release_notes(content(request, repository, sha, "CHANGELOG.md"), tag)


def select_stable(
    request,
    *,
    repository,
    tag,
    source_sha,
    peer_sha,
    controller_sha,
    default_branch,
    selected_ref,
    event,
    verify_only=False,
):
    controller(repository, controller_sha, default_branch, selected_ref, event)
    require(type(verify_only) is bool, "Invalid verification mode")
    version(tag, baseline=verify_only)
    require(
        valid_sha(source_sha) and valid_sha(peer_sha),
        "Stable sources must be full commit SHAs",
    )
    peer = next(repo for repo in BASELINES if repo != repository)
    baseline_rehearsal = verify_only and tag == BASELINE_TAG
    if baseline_rehearsal:
        require(
            source_sha == BASELINES[repository] and peer_sha == BASELINES[peer],
            "Read-only baseline verification requires the original pair",
        )
    for repo, sha in ((repository, source_sha), (peer, peer_sha)):
        if not baseline_rehearsal:
            branch = request(repo, "branches/" + quote("release/" + tag[1:], safe=""))
            require(
                branch.get("name") == "release/" + tag[1:]
                and branch.get("protected") is True
                and branch.get("commit", {}).get("sha") == sha,
                "Stable branch must be protected and still point at the selected commit",
            )
        verify_history(request, repo, sha)
        verify_metadata(request, repo, sha, tag)
    return {
        "channel": "stable-0.1",
        "controller_sha": controller_sha,
        "source_ref": "refs/heads/release/" + tag[1:],
        "baseline_tag": BASELINE_TAG,
        "baseline_sha": BASELINES[repository],
        "peer_repository": peer,
        "peer_sha": peer_sha,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    env = os.environ
    try:
        controller(
            env.get("GITHUB_REPOSITORY"),
            env.get("GITHUB_SHA"),
            env.get("DEFAULT_BRANCH"),
            env.get("GITHUB_REF"),
            env.get("GITHUB_EVENT_NAME"),
        )
        channel = env.get("RELEASE_CHANNEL", "default")
        source, peer = (
            env.get("RELEASE_SOURCE_SHA", ""),
            env.get("RELEASE_PEER_SHA", ""),
        )
        provenance = ""
        if channel == "default":
            require(
                not source and not peer and not args.verify_only,
                "Default releases cannot override application sources",
            )
            source = env["GITHUB_SHA"]
        else:
            require(
                channel == "stable-0.1" and env.get("RELEASE_MODE") == "release",
                "Stable source selection is restricted to release mode",
            )
            selected = select_stable(
                Reader(env.get("GH_TOKEN")).request,
                repository=env["GITHUB_REPOSITORY"],
                tag=env.get("RELEASE_TAG"),
                source_sha=source,
                peer_sha=peer,
                controller_sha=env["GITHUB_SHA"],
                default_branch=env["DEFAULT_BRANCH"],
                selected_ref=env["GITHUB_REF"],
                event=env["GITHUB_EVENT_NAME"],
                verify_only=args.verify_only,
            )
            provenance = json.dumps(selected, sort_keys=True, separators=(",", ":"))
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"source_sha={source}\nprovenance={provenance}\n")
    except (KeyError, TypeError, ValueError, OSError):
        parser.exit(
            1,
            "Stable release source verification failed; no publication is authorized.\n",
        )


if __name__ == "__main__":
    main()
