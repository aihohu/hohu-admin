"""Exercise the failed-next-series window with real isolated Git histories."""

import base64
import contextlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.ops import release_build, stable_release


@contextlib.contextmanager
def git_directory():
    directory = tempfile.TemporaryDirectory(prefix="stable-graph-")
    try:
        yield directory.name
    finally:
        # Windows scanners can briefly retain a completed Git process's files.
        # Retry cleanup within the owned temporary root; never ignore failure.
        assert (
            Path(directory.name).resolve().parent
            == Path(tempfile.gettempdir()).resolve()
        )
        for attempt in range(10):
            try:
                directory.cleanup()
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)


class StableGitGraphTests(unittest.TestCase):
    def test_hotfix_and_business_backport_preserve_both_series(self):
        with git_directory() as directory:
            roots, anchors, sources, controllers, fixes = {}, {}, {}, {}, {}

            def git(repo, *arguments):
                return subprocess.check_output(
                    ["git", *arguments],
                    cwd=roots[repo],
                    text=True,
                    encoding="utf-8",
                    stderr=subprocess.PIPE,
                ).strip()

            def metadata(repo, version):
                root = roots[repo]
                if repo.endswith("-web"):
                    (root / "package.json").write_text(
                        json.dumps({"version": version}), encoding="utf-8"
                    )
                else:
                    (root / "pyproject.toml").write_text(
                        f'[project]\nname="hohu-admin"\nversion="{version}"\n',
                        encoding="utf-8",
                    )
                    (root / "uv.lock").write_text(
                        '[[package]]\nname="hohu-admin"\n'
                        f'version="{version}"\nsource={{editable="."}}\n',
                        encoding="utf-8",
                    )
                (root / "CHANGELOG.md").write_text(
                    f"# Changes\n\n## [v{version}]\n\nReviewed repair.\n",
                    encoding="utf-8",
                )

            def commit(repo, message):
                names = ["CHANGELOG.md", "repair.txt"]
                names += (
                    ["package.json"]
                    if repo.endswith("-web")
                    else ["pyproject.toml", "uv.lock"]
                )
                git(repo, "add", "--", *names)
                git(repo, "commit", "-m", message)
                return git(repo, "rev-parse", "HEAD")

            for repo in stable_release.BASELINES:
                roots[repo] = Path(directory) / repo.split("/")[1]
                roots[repo].mkdir()
                git(repo, "init", "-b", "main")
                git(repo, "config", "user.name", "Release fixture")
                git(repo, "config", "user.email", "fixture@example.invalid")
                git(repo, "config", "commit.gpgsign", "false")
                git(repo, "config", "core.hooksPath", str(Path(directory) / "no-hooks"))
                metadata(repo, "0.1.6")
                (roots[repo] / "repair.txt").write_text("old\n", encoding="utf-8")
                anchors[repo] = commit(repo, "Published baseline fixture")
                git(repo, "tag", "-a", "v0.1.6", "-m", "Immutable baseline fixture")
                metadata(repo, "0.2.0")
                controllers[repo] = commit(repo, "Next series awaiting failed release")
                git(repo, "switch", "-c", "release/0.1.7", anchors[repo])
                (roots[repo] / "repair.txt").write_text("fixed\n", encoding="utf-8")
                fixes[repo] = commit(repo, "Business repair without version changes")
                metadata(repo, "0.1.7")
                sources[repo] = commit(repo, "Stable patch metadata")

            def request(repo, path):
                if path.startswith("branches/"):
                    # Protection is simulated; ancestry and all blobs are real Git.
                    return {
                        "name": "release/0.1.7",
                        "protected": True,
                        "commit": {"sha": git(repo, "rev-parse", "release/0.1.7")},
                    }
                if path == "releases/tags/v0.1.6":
                    return {"tag_name": "v0.1.6", "draft": False, "prerelease": False}
                if path == "git/ref/tags/v0.1.6":
                    return {
                        "object": {
                            "type": "tag",
                            "sha": git(repo, "rev-parse", "v0.1.6"),
                        }
                    }
                if path.startswith("git/tags/"):
                    return {
                        "tag": "v0.1.6",
                        "object": {
                            "type": "commit",
                            "sha": git(repo, "rev-parse", "v0.1.6^{commit}"),
                        },
                    }
                if path.startswith("compare/"):
                    base, sha = path.removeprefix("compare/").split("...")
                    ancestor = git(repo, "merge-base", base, sha)
                    return {
                        "status": "ahead" if ancestor == base else "diverged",
                        "merge_base_commit": {"sha": ancestor},
                    }
                name, sha = path.removeprefix("contents/").split("?ref=")
                raw = subprocess.check_output(
                    ["git", "show", f"{sha}:{name}"], cwd=roots[repo]
                )
                return {
                    "type": "file",
                    "encoding": "base64",
                    "size": len(raw),
                    "content": base64.b64encode(raw).decode(),
                }

            with patch.dict(stable_release.BASELINES, anchors, clear=True):
                for repo in roots:
                    peer = next(key for key in roots if key != repo)
                    provenance = stable_release.select_stable(
                        request,
                        repository=repo,
                        tag="v0.1.7",
                        source_sha=sources[repo],
                        peer_sha=sources[peer],
                        controller_sha=controllers[repo],
                        default_branch="main",
                        selected_ref="refs/heads/main",
                        event="workflow_dispatch",
                    )
                    identity = release_build.identity(
                        repo,
                        sources[repo],
                        "v0.1.7",
                        "123",
                        None,
                        provenance=provenance,
                    )
                    self.assertNotEqual(
                        identity["source_sha"],
                        release_build.controller_commit(identity),
                    )
                    git(repo, "switch", "main")
                    git(repo, "cherry-pick", fixes[repo])
                    self.assertEqual(
                        (roots[repo] / "repair.txt").read_text("utf-8"), "fixed\n"
                    )
                    self.assertEqual(
                        git(repo, "merge-base", controllers[repo], "HEAD"),
                        controllers[repo],
                    )
                    self.assertEqual(
                        git(repo, "rev-parse", "release/0.1.7"), sources[repo]
                    )
                    self.assertEqual(
                        git(repo, "rev-parse", "v0.1.6^{commit}"), anchors[repo]
                    )
                    stable_release.verify_metadata(
                        request, repo, git(repo, "rev-parse", "HEAD"), "v0.2.0"
                    )


if __name__ == "__main__":
    unittest.main()
