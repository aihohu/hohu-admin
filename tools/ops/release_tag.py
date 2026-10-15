"""Create immutable release tags owned by one GitHub Actions run."""

import argparse
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.ops.prepare_release import STABLE_TAG


class GitHub:
    def __init__(self, repository: str, token: str):
        if repository != "aihohu/hohu-admin":
            raise ValueError("Release tags are restricted to aihohu/hohu-admin")
        if not token:
            raise ValueError("GH_TOKEN is required")
        self.base = f"https://api.github.com/repos/{repository}/"
        self.token = token

    def request(self, method, path, payload=None, *, missing_ok=False):
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "hohu-admin-release",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if missing_ok and method == "GET" and error.code == 404:
                return None
            raise ValueError(
                f"GitHub API {method} failed (HTTP {error.code})"
            ) from None
        except (URLError, TimeoutError):
            raise ValueError("GitHub API connection failed") from None


def manage_tag(api, action, *, tag, sha, repository, run_id):
    if (
        STABLE_TAG.fullmatch(tag) is None
        or re.fullmatch(r"[0-9a-f]{40}", sha) is None
        or re.fullmatch(r"[1-9][0-9]*", run_id) is None
        or repository != "aihohu/hohu-admin"
        or action not in ("check", "create", "verify")
    ):
        raise ValueError(
            "Invalid release tag, source SHA, repository, run ID or action"
        )
    message = (
        f"Release {tag}\n\nGitHub Actions run: "
        f"https://github.com/{repository}/actions/runs/{run_id}\n"
    )
    ref = api.request("GET", f"git/ref/tags/{tag}", missing_ok=True)
    if ref is not None:
        obj = ref["object"]
        if obj["type"] != "tag":
            raise ValueError("Tag already exists outside this release run")
        existing = api.request("GET", f"git/tags/{obj['sha']}")
        if (
            existing["tag"] != tag
            or existing["object"]["type"] != "commit"
            or existing["object"]["sha"] != sha
            or existing["message"].strip() != message.strip()
        ):
            raise ValueError("Tag belongs to another run or source commit")
        return
    if action == "verify":
        raise ValueError("Release tag is missing")
    if action == "check":
        return
    created = api.request(
        "POST",
        "git/tags",
        {"tag": tag, "message": message, "object": sha, "type": "commit"},
    )
    # Never update or delete refs. A competing creator must fail this publication.
    api.request("POST", "git/refs", {"ref": f"refs/tags/{tag}", "sha": created["sha"]})
    manage_tag(api, "verify", tag=tag, sha=sha, repository=repository, run_id=run_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "create", "verify"))
    args = parser.parse_args()
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        manage_tag(
            GitHub(repository, os.environ.get("GH_TOKEN", "")),
            args.action,
            tag=os.environ["RELEASE_TAG"],
            sha=os.environ["SOURCE_SHA"],
            repository=repository,
            run_id=os.environ["GITHUB_RUN_ID"],
        )
    except (KeyError, ValueError) as error:
        parser.exit(1, f"Release tag check failed: {error}\n")


if __name__ == "__main__":
    main()
