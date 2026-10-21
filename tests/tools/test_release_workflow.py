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
    event="workflow_dispatch",
    repository="aihohu/hohu-admin",
    ref="refs/heads/main",
    mode="release",
    prepare="success",
    tag="success",
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
            prepare=ns(result=prepare, outputs=ns(mode=mode)),
            tag=ns(result=tag),
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
    def test_release_branch_ci_cannot_publish_even_with_successful_checks(self):
        for event in ("push", "pull_request", "workflow_call", "workflow_dispatch"):
            for mode in ("test", "release"):
                with self.subTest(event=event, mode=mode):
                    self.assertFalse(
                        enabled(
                            "docker",
                            event=event,
                            mode=mode,
                            ref="refs/heads/release/0.1.6",
                        )
                    )
        for event in ("push", "pull_request", "workflow_call", "pull_request_target"):
            docker_status = "success" if enabled("docker", event=event) else "skipped"
            for name in ("docker", "acr", "github-release"):
                with self.subTest(event=event, job=name):
                    self.assertFalse(enabled(name, event=event, docker=docker_status))
        self.assertIn("SELECTED_REF: ${{ github.ref }}", job("prepare"))
        self.assertIn(
            "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}",
            job("prepare"),
        )
        self.assertIn(
            'if [[ "$SELECTED_REF" != "refs/heads/$DEFAULT_BRANCH" ]]', job("prepare")
        )
        for name in ("tag", "docker", "acr"):
            self.assertIn("environment: docker", job(name))
        self.assertNotIn("secrets:", job("ci"))

    def test_web_release_requires_ci_and_release_qualification(self):
        self.assertNotIn("\n  release:", WORKFLOW)
        self.assertNotIn("\n  push:", WORKFLOW)
        self.assertIn("default: test", WORKFLOW)
        self.assertIn("needs: prepare", job("ci"))
        self.assertIn("uses: ./.github/workflows/ci.yml", job("ci"))
        self.assertIn("needs: ci", job("qualification"))
        self.assertIn("--profile release", job("qualification"))
        self.assertIn("needs: qualification", job("tag"))
        self.assertIn("needs: [prepare, qualification, tag]", job("docker"))
        self.assertIn("needs: [prepare, docker, acr]", job("github-release"))
        for status in ("failure", "cancelled", "skipped"):
            self.assertFalse(enabled("docker", qualification=status))
            self.assertFalse(enabled("docker", tag=status))
            self.assertFalse(enabled("docker", prepare=status))
        self.assertTrue(enabled("docker"))
        self.assertFalse(enabled("docker", cancelled=True))
        self.assertFalse(enabled("docker", repository="someone/fork"))
        self.assertFalse(enabled("docker", event="push"))
        self.assertFalse(enabled("docker", mode="unknown"))
        for ref in ("refs/heads/feature", "refs/tags/v0.1.5"):
            self.assertFalse(enabled("docker", ref=ref))
        self.assertEqual(WORKFLOW.count("value=${{ inputs.version }}"), 0)
        self.assertEqual(WORKFLOW.count("value=${{ needs.prepare.outputs.tag }}"), 2)

    def test_manual_test_build_only_accepts_default_branch_and_never_announces(self):
        self.assertTrue(
            enabled(
                "docker",
                event="workflow_dispatch",
                ref="refs/heads/main",
                qualification="skipped",
                tag="skipped",
                mode="test",
            )
        )
        for ref in ("refs/heads/feature", "refs/tags/v0.1.5"):
            self.assertFalse(
                enabled(
                    "docker",
                    event="workflow_dispatch",
                    ref=ref,
                    qualification="skipped",
                    tag="skipped",
                    mode="test",
                )
            )
        self.assertFalse(enabled("github-release", mode="test"))
        self.assertFalse(enabled("docker", mode="test", prepare="failure"))

    def test_announcement_waits_for_enabled_mirror_and_successful_build(self):
        self.assertTrue(enabled("github-release"))
        self.assertTrue(enabled("github-release", mirror="", acr="skipped"))
        for status in ("failure", "cancelled", "skipped"):
            self.assertFalse(enabled("github-release", acr=status))
            self.assertFalse(enabled("github-release", docker=status))
        self.assertFalse(enabled("github-release", mirror="false", acr="failure"))
        self.assertFalse(enabled("github-release", cancelled=True))
        self.assertFalse(enabled("github-release", repository="someone/fork"))
        self.assertFalse(enabled("github-release", event="push"))

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
python3() {
  test "$MOCK_SHA" = "$SOURCE_SHA"
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
