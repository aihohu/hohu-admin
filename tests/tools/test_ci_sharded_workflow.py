"""Required-check identities and event/source boundaries for file shards."""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.tools.test_ci_workflow import section

ROOT = Path(__file__).resolve().parents[2]


class ShardedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.jobs = section(self.ci, "jobs")

    def test_required_aggregates_fail_closed_and_hosted_waits(self):
        aggregate = section(self.jobs, "pytest", 2)
        self.assertIn("if: always()", aggregate)
        self.assertIn("needs: [test-plan, test-shards, pinned-tests]", aggregate)
        self.assertIn("needs.test-plan.result", aggregate)
        self.assertIn("needs.test-shards.result", aggregate)
        self.assertIn("needs.pinned-tests.result", aggregate)
        self.assertIn("tools.ci.sharding aggregate", aggregate)
        self.assertIn("--dependency", aggregate)
        self.assertIn("if-no-files-found: error", aggregate)
        hosted = section(self.jobs, "qualification", 2)
        self.assertIn("needs: [ruff, pytest]", hosted)
        self.assertNotIn("if: always()", hosted.split("    steps:", 1)[0])

    def test_groups_have_independent_resources_and_precheck_only_in_plan(self):
        worker = section(self.jobs, "test-shards", 2)
        plan = section(self.jobs, "test-plan", 2)
        self.assertIn("group: [0, 1, 2, 3]", worker)
        for job in (worker, plan):
            self.assertIn("image: postgres:15", job)
            self.assertIn("image: redis:7", job)
            self.assertIn("UV_PYTHON: ${{ matrix.python-version }}", job)
            self.assertIn("fail-fast: false", job)
        self.assertNotIn("Check transfer HTTP contracts", worker)
        self.assertEqual(plan.count("Check transfer HTTP contracts"), 1)
        self.assertIn("tools.ci.sharding collect", plan)
        self.assertIn("tools.ci.sharding plan", plan)
        self.assertIn("--plan", worker)
        self.assertIn("--group", worker)

    def test_pinned_source_keeps_original_complete_serial_gate(self):
        pinned = section(self.jobs, "pinned-tests", 2)
        self.assertIn("if: inputs.source_sha != ''", pinned)
        self.assertIn("ref: ${{ inputs.source_sha || github.sha }}", pinned)
        self.assertIn(
            "uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=70",
            pinned,
        )
        self.assertNotIn("tools.ci.sharding", pinned)
        self.assertIn(
            '[[ "$(git rev-parse HEAD)" == "$REQUESTED_SOURCE_SHA" ]]', pinned
        )
        self.assertIn("needs: ruff", pinned)
        self.assertIn("workflow_dispatch:", self.ci)
        self.assertIn('[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]', self.ci)
        for name in ("test-plan", "test-shards"):
            self.assertIn("if: inputs.source_sha == ''", section(self.jobs, name, 2))
        # Both real controllers still pass the validated application SHA.
        for filename in ("release.yml", "stable-verify.yml"):
            workflow = (ROOT / ".github/workflows" / filename).read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "source_sha: ${{ needs.prepare.outputs.source_sha }}", workflow
            )

    def test_only_ordinary_prs_replace_older_runs(self):
        for filename in ("ci.yml", "docs.yml"):
            workflow = (ROOT / ".github/workflows" / filename).read_text(
                encoding="utf-8"
            )
            concurrency = section(workflow, "concurrency")
            self.assertIn("github.event_name == 'pull_request'", concurrency)
            self.assertIn("github.event.pull_request.number", concurrency)
            self.assertIn("github.run_id", concurrency)
            self.assertIn(
                "startsWith(github.event.pull_request.head.ref, 'release/')",
                concurrency,
            )
            self.assertIn("github.event.pull_request.base.ref", concurrency)
            self.assertIn("cancel-in-progress:", concurrency)
        refs = re.findall(r"^\s+ref: (.+)$", self.ci, re.M)
        self.assertEqual(set(refs), {"${{ inputs.source_sha || github.sha }}"})

    def test_real_dependency_shell_rejects_unsuccessful_routes(self):
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
        aggregate = section(self.jobs, "pytest", 2)
        script = textwrap.dedent(
            aggregate.split("        run: |\n", 1)[1].split("\n      - ", 1)[0]
        )
        for source in ("", "a" * 40):
            for plan in ("success", "failure", "skipped", "cancelled"):
                for group in ("success", "failure", "skipped", "cancelled"):
                    for pinned in ("success", "failure", "skipped", "cancelled"):
                        outcome = subprocess.run(
                            [bash, "-c", script],
                            env=os.environ
                            | {
                                "SELECTED_SOURCE": source,
                                "PLAN_RESULT": plan,
                                "GROUP_RESULT": group,
                                "PINNED_RESULT": pinned,
                            },
                            capture_output=True,
                            timeout=10,
                        )
                        expected = (
                            pinned == "success"
                            if source
                            else plan == group == "success"
                        )
                        self.assertEqual(
                            outcome.returncode == 0,
                            expected,
                            (source, plan, group, pinned),
                        )

    def test_dispatch_rejects_non_commit_input_before_source_work(self):
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
        ruff = section(self.jobs, "ruff", 2)
        script = textwrap.dedent(
            ruff.split("        run: |\n", 1)[1].split("\n      - ", 1)[0]
        )
        for event, source, success in (
            ("pull_request", "", True),
            ("push", "", True),
            ("workflow_dispatch", "a" * 40, True),
            ("workflow_dispatch", "", False),
            ("workflow_dispatch", "main", False),
            ("workflow_dispatch", "a" * 40 + "\n", False),
            ("push", "abc", False),
        ):
            outcome = subprocess.run(
                [bash, "-c", script],
                env=os.environ
                | {
                    "EVENT_NAME": event,
                    "SOURCE_SHA": source,
                },
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(outcome.returncode == 0, success, (event, source))

    def test_manual_old_source_results_cannot_satisfy_pr_required_names(self):
        prefix = "${{ github.event_name == 'workflow_dispatch' && github.workflow == 'CI' && 'Selected source / ' || '' }}"
        for name, label in (
            ("ruff", "Ruff"),
            ("pytest", "Pytest (Python ${{ matrix.python-version }})"),
            ("qualification", "Hosted release qualification (CI profile)"),
        ):
            self.assertIn("    name: " + prefix + label, section(self.jobs, name, 2))
        self.assertTrue(self.ci.startswith("name: CI\n"))
        for filename in ("release.yml", "stable-verify.yml"):
            controller = (ROOT / ".github/workflows" / filename).read_text(
                encoding="utf-8"
            )
            self.assertNotEqual(controller.splitlines()[0], "name: CI")

    def test_old_source_without_new_transfer_files_still_runs_full_gate(self):
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
        pinned = section(self.jobs, "pinned-tests", 2)
        precheck = pinned.split(
            "      - name: Check selected source transfer contracts when present\n", 1
        )[1].split("\n      - name:", 1)[0]
        full = pinned.split(
            "      - name: Run the selected source complete suite and coverage gate\n",
            1,
        )[1].split("\n      - name:", 1)[0]
        script = (
            textwrap.dedent(precheck.split("        run: |\n", 1)[1])
            + "\n"
            + textwrap.dedent(full.split("        run: |\n", 1)[1])
        )
        for present in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if present:
                    for file in (
                        "tests/core/test_opaque_field_identity.py",
                        "tests/modules/system/test_generic_transfer_http.py",
                    ):
                        path = root / file
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.touch()
                harness = (
                    "uv() { printf '%s\\n' \"$*\" >> calls; }\n"
                    "git() { printf '%s\\n' \"$REQUESTED_SOURCE_SHA\"; }\n"
                )
                outcome = subprocess.run(
                    [bash, "-c", harness + script],
                    cwd=root,
                    env=os.environ
                    | {"RUNNER_TEMP": directory, "REQUESTED_SOURCE_SHA": "a" * 40},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(len(calls), 3 if present else 2)
                self.assertIn(
                    "pytest --cov=app --cov-report=term-missing --cov-fail-under=70",
                    calls[-1],
                )


if __name__ == "__main__":
    unittest.main()
