"""Real subprocesses exercise collection, artifacts and branch data merging."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ShardingExecutionTests(unittest.TestCase):
    def test_real_workers_aggregate_and_reject_missing_or_corrupt_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app").mkdir()
            (root / "tests").mkdir()
            (root / "app/__init__.py").touch()
            (root / "app/branch.py").write_text(
                "def choose(flag):\n    if flag:\n        return 1\n    return 2\n"
            )
            for name, flag, expected in (("a", True, 1), ("b", False, 2)):
                (root / f"tests/test_{name}.py").write_text(
                    "from app.branch import choose\n"
                    "def test_branch(subtests):\n"
                    "    for index in range(2):\n"
                    "        with subtests.test(index=index):\n"
                    f"            assert choose({flag}) == {expected}\n"
                )
            (root / "uv.lock").write_text("# fixture\n")
            (root / "pyproject.toml").write_text(
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                '[tool.coverage.run]\nsource = ["app"]\nbranch = true\n'
            )
            env = os.environ | {
                "PYTHONPATH": str(root) + os.pathsep + str(ROOT),
                "GITHUB_RUN_ID": "fixture",
                "GITHUB_RUN_ATTEMPT": "1",
            }

            # This repo is disposable; no global Git config or real service access.
            def run(command, success=True):
                result = subprocess.run(
                    command,
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode == 0, success, result.stdout + result.stderr
                )
                return result

            run(["git", "init", "-q"])
            run(["git", "add", "app", "tests", "uv.lock", "pyproject.toml"])
            run(
                [
                    "git",
                    "-c",
                    "user.name=CI fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-qm",
                    "fixture",
                ]
            )
            cli = [sys.executable, "-m", "tools.ci.sharding"]
            run(cli + ["collect", "--output", "collect"])
            run(
                cli
                + [
                    "plan",
                    "--input",
                    "collect/report.json",
                    "--groups",
                    "2",
                    "--output",
                    "plan.json",
                ]
            )
            for group in (0, 1):
                run(
                    cli
                    + [
                        "run",
                        "--plan",
                        "plan.json",
                        "--group",
                        str(group),
                        "--output",
                        f"workers/{group}",
                    ]
                )
            aggregate = cli + [
                "aggregate",
                "--plan",
                "plan.json",
                "--input",
                "workers",
                "--output",
                "combined",
                "--dependency",
                "success",
            ]
            run(aggregate)
            summary = json.loads((root / "combined/summary.json").read_text())
            self.assertEqual(summary["tests"], 2)
            self.assertEqual(summary["subtests"], 4)
            self.assertEqual(summary["coverage"], 100)
            for dependency in ("skipped", "failure", "cancelled"):
                run(aggregate[:-1] + [dependency], success=False)
            report = root / "workers/1/report.json"
            original = report.read_bytes()
            report.unlink()
            run(aggregate, success=False)
            report.write_bytes(original)
            data = root / "workers/1/coverage.data"
            data.write_bytes(b"broken")
            run(aggregate, success=False)


if __name__ == "__main__":
    unittest.main()
