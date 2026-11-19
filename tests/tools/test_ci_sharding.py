"""Fail-closed CI partition and evidence contracts, without shared services."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.ci.sharding import build_plan, validate_reports

IDENTITY = {
    "sha": "a" * 40,
    "tree": "b" * 40,
    "python": "3.12",
    "run": "12",
    "attempt": "1",
}
NODES = [
    "tests/test_a.py::test_second",
    "tests/test_a.py::test_first",
    "tests/test_b.py::test_one",
    "tests/test_c.py::test_one",
]


def fixture():
    plan = build_plan(NODES, IDENTITY, {"tests/test_a.py": 90}, 2)
    reports = []
    for index, nodes in enumerate(plan["groups"]):
        reports.append(
            {
                "identity": IDENTITY,
                "group": index,
                "inventory": NODES,
                "selected": nodes,
                "exitcode": 0,
                "phases": [
                    {"nodeid": node, "when": phase, "outcome": "passed", "seconds": 1}
                    for node in nodes
                    for phase in ("setup", "call", "teardown")
                ],
                "subtests": [],
            }
        )
    return plan, reports


class PartitionTests(unittest.TestCase):
    def test_file_atomicity_and_original_order(self):
        plan, _ = fixture()
        self.assertIn(NODES[:2], plan["groups"])
        self.assertEqual(sorted(sum(plan["groups"], [])), sorted(NODES))
        self.assertEqual(
            build_plan(NODES, IDENTITY, {}, 2), build_plan(NODES, IDENTITY, {}, 2)
        )

    def test_invalid_collection_or_group_count_is_rejected(self):
        for nodes, count in (
            ([], 2),
            (NODES + NODES[:1], 2),
            (NODES, 0),
            (NODES, 4),
            (["../bad.py::test_x"], 1),
        ):
            with self.subTest(nodes=nodes, count=count), self.assertRaises(ValueError):
                build_plan(nodes, IDENTITY, {}, count)

    def test_historical_weights_change_assignment_not_coverage(self):
        plan = build_plan(NODES, IDENTITY, {"tests/test_b.py": 900}, 2)
        self.assertIn([NODES[2]], plan["groups"])
        self.assertEqual(sorted(sum(plan["groups"], [])), sorted(NODES))


class EvidenceTests(unittest.TestCase):
    def test_success_checks_exact_nodes_and_subtests(self):
        plan, reports = fixture()
        reports[0]["subtests"] = [{"outcome": "passed", "nodeid": NODES[0]}]
        result = validate_reports(plan, reports, "success")
        self.assertEqual(result["tests"], 4)
        self.assertEqual(result["subtests"], 1)

    def test_dependency_failure_skip_cancel_and_missing_artifacts_fail(self):
        plan, reports = fixture()
        for status in ("failure", "skipped", "cancelled", "", "unknown"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                validate_reports(plan, reports, status)
        with self.assertRaises(ValueError):
            validate_reports(plan, reports[:1], "success")

    def test_identity_mismatch_and_duplicate_group_fail(self):
        for key in IDENTITY:
            plan, reports = fixture()
            reports[0]["identity"] = IDENTITY | {key: "different"}
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_reports(plan, reports, "success")
        plan, reports = fixture()
        with self.assertRaises(ValueError):
            validate_reports(plan, [reports[0], reports[0]], "success")

    def test_execution_omissions_duplicates_failed_phases_and_skips_fail(self):
        for mutate in (
            lambda r: r.update(exitcode=1),
            lambda r: r["selected"].pop(),
            lambda r: r["selected"].reverse(),
            lambda r: r["inventory"].pop(),
            lambda r: r["phases"].pop(),
            lambda r: r["phases"].append(copy.deepcopy(r["phases"][0])),
            lambda r: r["phases"][0].update(outcome="failed"),
            lambda r: r["phases"][0].update(outcome="skipped"),
            lambda r: r["subtests"].append({"nodeid": NODES[0], "outcome": "failed"}),
        ):
            plan, reports = fixture()
            reports = copy.deepcopy(reports)
            mutate(reports[0])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate_reports(plan, reports, "success")

    def test_tampered_plan_omits_or_duplicates_nodes(self):
        for duplicate in (False, True):
            plan, reports = fixture()
            if duplicate:
                plan["groups"][1] += plan["groups"][0]
            else:
                plan["groups"][0].pop()
            with self.assertRaises(ValueError):
                validate_reports(plan, reports, "success")

    def test_serialized_contract_round_trip(self):
        plan, reports = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            validate_reports(json.loads(path.read_text()), reports, "success")


if __name__ == "__main__":
    unittest.main()
