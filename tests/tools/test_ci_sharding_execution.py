"""Real subprocesses exercise collection, artifacts and branch data merging."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coverage import CoverageData

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
                    "import time\nimport pytest\n"
                    "from app.branch import choose\n"
                    '@pytest.mark.parametrize("blob", [str(time.time_ns()).encode() for _ in range(2)])\n'
                    "def test_branch(blob, subtests):\n"
                    "    assert blob\n"
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
            self.assertEqual(summary["tests"], 4)
            self.assertEqual(summary["subtests"], 8)
            self.assertEqual(summary["coverage"], 100)
            for dependency in ("skipped", "failure", "cancelled"):
                run(aggregate[:-1] + [dependency], success=False)
            report = root / "workers/1/report.json"
            original = report.read_bytes()
            report.unlink()
            run(aggregate, success=False)
            report.write_bytes(original)
            for name in ("junit.xml", "coverage.data", "pytest.log"):
                artifact = root / "workers/1" / name
                content = artifact.read_bytes()
                artifact.unlink()
                run(aggregate, success=False)
                artifact.write_bytes(b"broken")
                run(aggregate, success=False)
                artifact.write_bytes(content)
            # Even valid, hash-matching statement-only data cannot replace branch data.
            data_path = root / "workers/1/coverage.data"
            original_data = data_path.read_bytes()
            data_path.unlink()
            data = CoverageData(basename=str(data_path))
            data.add_lines({"app/branch.py": [1, 2, 3, 4]})
            data.write()
            metadata = json.loads(original)
            metadata["artifacts"]["coverage.data"] = hashlib.sha256(
                data_path.read_bytes()
            ).hexdigest()
            report.write_text(json.dumps(metadata))
            run(aggregate, success=False)
            data_path.write_bytes(original_data)
            report.write_bytes(original)
            # Real valid branch files below 70% must fail after combining.
            for group in (0, 1):
                folder = root / f"workers/{group}"
                (folder / "coverage.data").unlink()
                data = CoverageData(basename=str(folder / "coverage.data"))
                data.add_arcs({"app/branch.py": [(-1, 1), (1, -1)]})
                data.write()
                metadata = json.loads((folder / "report.json").read_text())
                metadata["artifacts"]["coverage.data"] = hashlib.sha256(
                    (folder / "coverage.data").read_bytes()
                ).hexdigest()
                (folder / "report.json").write_text(json.dumps(metadata))
            failed = run(aggregate, success=False)
            self.assertIn("Combined branch coverage below 70%", failed.stderr)


if __name__ == "__main__":
    unittest.main()
