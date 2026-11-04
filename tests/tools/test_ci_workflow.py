"""Guard CI branch coverage and privilege boundaries without contacting GitHub.

These checks intentionally cover the workflow's simple filter shape. GitHub's
actual PR scheduling and required-check enforcement need separate acceptance.
"""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


DEFAULT_BRANCHES = ("main", "master")
WORKFLOWS = ("ci.yml", "docs.yml")
JOBS = {
    "ruff": "Ruff",
    "pytest": "Pytest (Python ${{ matrix.python-version }})",
    "qualification": "Hosted release qualification (CI profile)",
}
REQUIRED_COMMANDS = (
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run python -m tools.checks.check_ai_tools",
    "uv run python -m tools.checks.check_docs",
    "uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=70",
    'python-version: ["3.12", "3.13", "3.14"]',
    "needs: [ruff, pytest]",
    "--profile ci",
)


def section(text, name, indent=0):
    prefix = " " * indent
    match = re.search(
        rf"^{prefix}{re.escape(name)}:\n(.*?)(?=^{prefix}\S|\Z)",
        text,
        re.M | re.S,
    )
    if match is None:
        raise AssertionError(f"Missing workflow section: {name}")
    return match[1]


def branch_filters(workflow, event):
    block = section(section(workflow, "on"), event, 2)
    # Allow no paths/tags/types filters that can leave required checks pending.
    match = re.fullmatch(r"    branches: \[(.*?)\]\s*", block)
    if match is None:
        raise AssertionError(f"Unexpected filters on {event}: {block}")
    return [item.strip().strip("'").strip('"') for item in match[1].split(",")]


def matches_branch(branch, patterns):
    # GitHub's single * does not match /. Only this simple pattern is permitted.
    return any(
        re.fullmatch(re.escape(pattern).replace(r"\*", "[^/]*"), branch)
        for pattern in patterns
    )


class CIWorkflowTests(unittest.TestCase):
    def test_database_initialization_requires_successful_migration(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
        step = ci.split(
            "      - name: Initialize the selected source in the ephemeral test database\n",
            1,
        )[1].split("\n      - name:", 1)[0]
        script = textwrap.dedent(step.split("run: |\n", 1)[1])
        bash = (
            str(
                Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                / "Git/bin/bash.exe"
            )
            if os.name == "nt"
            else shutil.which("bash")
        )
        if not bash or not Path(bash).is_file():
            self.skipTest("Workflow shell requires Bash")
        for maintenance, migration_failure, seed_failure in (
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (True, True, False),
            (True, False, True),
        ):
            with (
                self.subTest(
                    maintenance=maintenance,
                    migration_failure=migration_failure,
                    seed_failure=seed_failure,
                ),
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
            ):
                cwd = Path(directory)
                if maintenance:
                    (cwd / "tests/release").mkdir(parents=True)
                    (cwd / "tests/release/bootstrap_database.py").touch()
                harness = """
                uv() {
                  printf '%s\\n' "$*" >> calls
                  if [[ "$*" == 'run alembic upgrade head' ]]; then
                    [[ "$MIGRATION_FAILURE" == 0 ]] || return 19
                    touch migrated
                  else
                    [[ -f migrated ]] || return 23
                    [[ "$SEED_FAILURE" == 0 ]] || return 29
                  fi
                }
                git() { printf '%040d\\n' 1; }
                """
                outcome = subprocess.run(
                    [bash, "-c", textwrap.dedent(harness) + script],
                    cwd=cwd,
                    env=os.environ
                    | {
                        "RUNNER_TEMP": directory,
                        "MIGRATION_FAILURE": str(int(migration_failure)),
                        "SEED_FAILURE": str(int(seed_failure)),
                    },
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(
                    outcome.returncode,
                    19 if migration_failure else 29 if seed_failure else 0,
                    outcome.stderr,
                )
                calls = (cwd / "calls").read_text("utf-8").splitlines()
                self.assertEqual(calls[0], "run alembic upgrade head")
                self.assertEqual(len(calls), 1 if migration_failure else 2)
                if not migration_failure:
                    seed = (
                        "run python -m tests.release.bootstrap_database "
                        if maintenance
                        else "run python -m scripts.init_db"
                    )
                    self.assertTrue(calls[1].startswith(seed), calls)

    def test_ai_static_check_uses_test_settings_on_a_clean_runner(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
        step = ci.split("      - name: Check AI tools\n", 1)[1].split("\n      - ", 1)[
            0
        ]
        self.assertIn("        env:\n          ENV: test\n", step)

    def test_documentation_job_retains_real_checks_without_skip_conditions(self):
        docs = (ROOT / ".github/workflows/docs.yml").read_text("utf-8")
        jobs = section(docs, "jobs")
        self.assertEqual(set(re.findall(r"^  ([\w-]+):", jobs, re.M)), {"public-docs"})
        self.assertNotIn("continue-on-error", jobs)
        self.assertNotRegex(jobs, r"(?m)^ +if:")
        self.assertIn("python -m tools.checks.check_docs", jobs)
        self.assertIn("unittest discover -s tests/tools -p test_check_docs.py", jobs)

    def test_ci_keeps_default_branches_and_adds_release_push_and_pr_base(self):
        for filename in WORKFLOWS:
            workflow = (ROOT / ".github/workflows" / filename).read_text("utf-8")
            triggers = section(workflow, "on")
            expected_events = {"push", "pull_request"}
            if filename == "ci.yml":
                expected_events.add("workflow_call")
                self.assertRegex(triggers, r"(?m)^  workflow_call:\s*$")
            self.assertEqual(
                set(re.findall(r"^  ([\w_]+):", triggers, re.M)), expected_events
            )
            for event in ("push", "pull_request"):
                patterns = branch_filters(workflow, event)
                self.assertEqual(set(patterns), set(DEFAULT_BRANCHES) | {"release/*"})
                for branch, expected in (
                    *((branch, True) for branch in DEFAULT_BRANCHES),
                    ("release/0.1.6", True),
                    ("release/0.2.0", True),
                    ("feat/v016-0", False),
                    ("fix/tenant", False),
                    ("releases/0.1.6", False),
                    ("release/0.1.6/nested", False),
                ):
                    with self.subTest(workflow=filename, event=event, branch=branch):
                        self.assertEqual(
                            bool(matches_branch(branch, patterns)), expected
                        )
            self.assertNotIn("github.head_ref", workflow)

    def test_ci_has_only_read_token_without_release_environment_or_secrets(self):
        for filename in WORKFLOWS:
            workflow = (ROOT / ".github/workflows" / filename).read_text("utf-8")
            self.assertEqual(section(workflow, "permissions").strip(), "contents: read")
            self.assertNotRegex(workflow, r"(?m)^ +permissions:")
            self.assertNotRegex(workflow, r"(?m)^ +environment:")
            self.assertNotRegex(workflow, r"\bsecrets\s*[.:\[]")
            self.assertNotIn("write-all", workflow)
            self.assertNotIn("docker/login-action", workflow)
            self.assertNotIn("./.github/workflows/release.yml", workflow)
            checkouts = re.findall(
                r"uses: actions/checkout@v\d+\n(.*?)(?=\n\s+- |\n  [\w-]+:|\Z)",
                workflow,
                re.S,
            )
            self.assertTrue(checkouts)
            for checkout in checkouts:
                source = (
                    "inputs.source_sha || github.sha"
                    if filename == "ci.yml"
                    else "github.sha"
                )
                self.assertIn("ref: " + "$" + "{{ " + source + " }}", checkout)
                self.assertIn("persist-credentials: false", checkout)

    def test_required_jobs_cannot_skip_release_branches_or_ignore_failures(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
        jobs = section(ci, "jobs")
        self.assertEqual(set(re.findall(r"^  ([\w-]+):", jobs, re.M)), set(JOBS))
        self.assertNotIn("continue-on-error", ci)
        self.assertNotRegex(ci, r"(?m)^    if:")
        self.assertNotRegex(ci, r"(?m)^ *if: (?!always\(\)$)")
        for name, label in JOBS.items():
            self.assertIn(f"    name: {label}\n", section(jobs, name, 2))
        for command in REQUIRED_COMMANDS:
            self.assertIn(command, ci)


if __name__ == "__main__":
    unittest.main()
