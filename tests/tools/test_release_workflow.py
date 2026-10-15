"""Exercise release failure gates and retry behavior without publishing anything."""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")


def job(name):
    return re.search(rf"^  {name}:\n(.*?)(?=^  [\w-]+:|\Z)", WORKFLOW, re.M | re.S)[1]


def enabled(
    name,
    *,
    event="push",
    repository="aihohu/hohu-admin",
    ref="refs/tags/v0.1.5",
    qualification="success",
    docker="success",
    acr="success",
    mirror="true",
    cancelled=False,
):
    expression = re.search(r"^    if: >-\n(.*?)(?=^    \w)", job(name), re.M | re.S)[1]
    expression = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    # These jobs must handle skipped ancestors explicitly, rather than implicitly
    # gaining Actions' success() condition and losing the manual/mirror-off paths.
    if not re.search(r"\b(?:always|cancelled|success|failure)\(\)", expression):
        raise AssertionError("Missing explicit Actions status check")
    expression = (
        " ".join(expression.split()).replace("&&", " and ").replace("||", " or ")
    )
    expression = re.sub(r"!(?!=)", "not ", expression)
    ns = SimpleNamespace
    context = {
        "github": ns(
            repository=repository,
            event_name=event,
            ref=ref,
            event=ns(repository=ns(default_branch="main")),
        ),
        "needs": ns(
            qualification=ns(result=qualification),
            docker=ns(result=docker),
            acr=ns(result=acr),
        ),
        "vars": ns(ACR_MIRROR_ENABLED=mirror),
        "cancelled": lambda: cancelled,
        "always": lambda: True,
        "format": lambda pattern, value: pattern.format(value),
    }
    return bool(eval(expression, {"__builtins__": {}}, context))


class ReleaseWorkflowTests(unittest.TestCase):
    def test_tag_release_requires_ci_and_release_qualification(self):
        self.assertNotIn("\n  release:", WORKFLOW)
        self.assertIn("tags: ['v*']", WORKFLOW)
        self.assertIn("needs: prepare", job("ci"))
        self.assertIn("uses: ./.github/workflows/ci.yml", job("ci"))
        self.assertIn("needs: ci", job("qualification"))
        self.assertIn("--profile release", job("qualification"))
        self.assertIn("needs: qualification", job("docker"))
        self.assertIn("needs: [docker, acr]", job("github-release"))
        for status in ("failure", "cancelled", "skipped"):
            self.assertFalse(enabled("docker", qualification=status))
        self.assertTrue(enabled("docker"))
        self.assertFalse(enabled("docker", cancelled=True))
        self.assertFalse(enabled("docker", repository="someone/fork"))

    def test_manual_test_build_only_accepts_default_branch_and_never_announces(self):
        self.assertTrue(
            enabled(
                "docker",
                event="workflow_dispatch",
                ref="refs/heads/main",
                qualification="skipped",
            )
        )
        for ref in ("refs/heads/feature", "refs/tags/v0.1.5"):
            self.assertFalse(
                enabled(
                    "docker",
                    event="workflow_dispatch",
                    ref=ref,
                    qualification="skipped",
                )
            )
        self.assertFalse(enabled("github-release", event="workflow_dispatch"))

    def test_announcement_waits_for_enabled_mirror_and_successful_build(self):
        self.assertTrue(enabled("github-release"))
        self.assertTrue(enabled("github-release", mirror="", acr="skipped"))
        for status in ("failure", "cancelled", "skipped"):
            self.assertFalse(enabled("github-release", acr=status))
            self.assertFalse(enabled("github-release", docker=status))
        self.assertFalse(enabled("github-release", mirror="false", acr="failure"))
        self.assertFalse(enabled("github-release", cancelled=True))
        self.assertFalse(enabled("github-release", repository="someone/fork"))

    def test_publication_retries_preserve_announcements_and_reject_moved_tags(self):
        bash = (
            shutil.which("bash")
            if os.name != "nt"
            else "C:/Program Files/Git/bin/bash.exe"
        )
        if not bash or not Path(bash).is_file():
            self.skipTest("Bash is required for shell workflow regression")
        script = textwrap.dedent(job("github-release").rsplit("        run: |\n", 1)[1])
        mocks = """
git() {
  if [[ "$1" == 'rev-parse' ]]; then
    echo "$MOCK_SHA"
  fi
}
gh() {
  echo "$*" >> "$CALLS"
  if [[ "$2" == 'view' ]]; then
    case "$MODE" in
      new) return 1 ;;
      draft) echo true ;;
      existing) echo false ;;
    esac
  fi
}
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "publish.sh"
            path.write_text(mocks + script, encoding="utf-8", newline="\n")
            for mode, sha, expected in (
                ("new", "a" * 40, "release create"),
                ("draft", "a" * 40, "release edit"),
                ("existing", "a" * 40, None),
                ("new", "b" * 40, None),
            ):
                with self.subTest(mode=mode, sha=sha):
                    calls = root / f"{mode}-{sha}.txt"
                    env = {
                        **os.environ,
                        "MODE": mode,
                        "MOCK_SHA": sha,
                        "SOURCE_SHA": "a" * 40,
                        "RELEASE_TAG": "v0.1.5",
                        "CALLS": calls.as_posix(),
                    }
                    result = subprocess.run(
                        [bash, path.as_posix()],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0 if sha == "a" * 40 else 1, result.stderr
                    )
                    commands = calls.read_text() if calls.exists() else ""
                    if expected:
                        self.assertIn(expected, commands)
                        self.assertIn(
                            "--notes-file .local/artifacts/release-notes.md", commands
                        )
                    else:
                        self.assertNotIn("release create", commands)
                        self.assertNotIn("release edit", commands)


if __name__ == "__main__":
    unittest.main()
