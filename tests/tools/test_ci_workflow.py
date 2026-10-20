"""Guard CI branch coverage and privilege boundaries without contacting GitHub.

These checks intentionally cover the workflow's simple filter shape. GitHub's
actual PR scheduling and required-check enforcement need separate acceptance.
"""

import re
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
                self.assertIn("ref: " + "$" + "{{ github.sha }}", checkout)
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
