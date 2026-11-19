"""File-atomic partitions and fail-closed, per-Python coverage aggregation."""

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from coverage import Coverage, CoverageData


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity():
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    return {
        "sha": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "app_tree": git("rev-parse", "HEAD:app"),
        "tests_tree": git("rev-parse", "HEAD:tests"),
        "lock": sha256(Path("uv.lock")),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_full": platform.python_version(),
        "run": os.environ.get("GITHUB_RUN_ID", "local"),
        "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
    }


def validate_nodes(nodes):
    require(isinstance(nodes, list) and bool(nodes), "Empty collection")
    require(len(set(nodes)) == len(nodes), "Duplicate nodeid")
    for node in nodes:
        file = node.split("::", 1)[0]
        require(
            file.startswith("tests/")
            and file.endswith(".py")
            and ".." not in file.split("/")
            and "\\" not in file,
            "Invalid test path",
        )


def build_plan(nodes, source, history, count):
    validate_nodes(nodes)
    files = list(dict.fromkeys(node.split("::", 1)[0] for node in nodes))
    require(type(count) is int and 1 <= count <= len(files), "Invalid group count")
    require(
        all(
            isinstance(v, (int, float)) and math.isfinite(v) and v >= 0
            for v in history.values()
        ),
        "Invalid historical duration",
    )
    weights = {f: max(history.get(f, 60.0), 0.01) for f in files}
    groups, totals = [[] for _ in range(count)], [0.0] * count
    assignment = {}
    for file in sorted(files, key=lambda f: (-weights[f], f)):
        target = min(range(count), key=lambda i: (totals[i], i))
        assignment[file] = target
        totals[target] += weights[file]
    for node in nodes:
        groups[assignment[node.split("::", 1)[0]]].append(node)
    return {
        "schema": 1,
        "identity": source,
        "inventory": nodes,
        "groups": groups,
        "estimated_seconds": totals,
    }


def validate_reports(plan, reports, dependency):
    require(dependency == "success", "A required job failed, skipped or was cancelled")
    nodes = plan["inventory"]
    validate_nodes(nodes)
    groups = plan["groups"]
    require(bool(groups) and all(groups), "Empty group")
    require(
        Counter(sum(groups, [])) == Counter(nodes), "Plan omits or duplicates tests"
    )
    file_groups = {}
    for i, group in enumerate(groups):
        group_set = set(group)
        require(
            group == [n for n in nodes if n in group_set], "Changed collection order"
        )
        for node in group:
            file = node.split("::", 1)[0]
            require(file_groups.setdefault(file, i) == i, "File split across groups")
    require(len(reports) == len(groups), "Missing group artifact")
    require(
        Counter(r["group"] for r in reports) == Counter(range(len(groups))),
        "Duplicate or unexpected group",
    )
    subtests = 0
    for report in reports:
        require(report["identity"] == plan["identity"], "Source/Python/run mismatch")
        require(report["inventory"] == nodes, "Collection differs from full inventory")
        selected = groups[report["group"]]
        require(report["selected"] == selected, "Selected tests differ from plan")
        require(report["exitcode"] == 0, "pytest did not finish successfully")
        expected = Counter(
            (node, phase)
            for node in selected
            for phase in ("setup", "call", "teardown")
        )
        actual = Counter((p["nodeid"], p["when"]) for p in report["phases"])
        require(actual == expected, "Missing or repeated execution phase")
        require(
            all(p["outcome"] == "passed" for p in report["phases"]),
            "Failed or skipped test phase",
        )
        require(
            all(
                s["nodeid"] in selected and s["outcome"] == "passed"
                for s in report["subtests"]
            ),
            "Failed or unexpected subtest",
        )
        subtests += len(report["subtests"])
    return {"tests": len(nodes), "subtests": subtests, "groups": len(groups)}


def execute(mode, output, plan_path=None, group=0):
    """Use the selected checkout's interpreter, tests, config and dependencies."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"HOHU_CI_OUTPUT": str(output), "HOHU_CI_GROUP": str(group)}
    env.pop("HOHU_CI_PLAN", None)
    if plan_path:
        env["HOHU_CI_PLAN"] = str(plan_path.resolve())
    command = [sys.executable]
    if mode != "collect":
        config = Path("pyproject.toml").read_text(encoding="utf-8")
        require("relative_files" not in config, "Review coverage relative path config")
        config = config.replace(
            "[tool.coverage.run]", "[tool.coverage.run]\nrelative_files = true", 1
        )
        (output / "coverage.toml").write_text(config, encoding="utf-8")
        env["COVERAGE_FILE"] = str(output / "coverage.data")
        command += ["-m", "coverage", "run", "--rcfile", str(output / "coverage.toml")]
    command += [
        "-m",
        "pytest",
        "-p",
        "tools.ci.reporter",
        "tests",
        "--basetemp",
        str(output / "tmp"),
        "-o",
        f"cache_dir={output / 'cache'}",
    ]
    if mode == "collect":
        command += ["--collect-only", "-q"]
    else:
        command += ["--junitxml", str(output / "junit.xml"), "--durations=0"]
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        for line in process.stdout:
            log.write(line)
            print(line, end="", flush=True)
        code = process.wait()
    report_path = output / "report.json"
    if report_path.exists():
        report = read_json(report_path)
        report["process_exitcode"] = code
        report["artifacts"] = {
            name: sha256(output / name)
            for name in ("junit.xml", "coverage.data", "pytest.log")
            if (output / name).exists()
        }
        write_json(report_path, report)
    return code


def aggregate(plan_path, inputs, output, dependency):
    plan = read_json(plan_path)
    require(plan["identity"] == identity(), "Aggregator source/Python/run mismatch")
    paths = sorted(inputs.glob("*/report.json"))
    reports = [read_json(p) for p in paths]
    result = validate_reports(plan, reports, dependency)
    data_paths = []
    for path, report in zip(paths, reports, strict=True):
        require(report["process_exitcode"] == 0, "Subprocess failure")
        for name in ("junit.xml", "coverage.data", "pytest.log"):
            artifact = path.parent / name
            require(
                artifact.is_file() and sha256(artifact) == report["artifacts"][name],
                "Missing or corrupted artifact: " + name,
            )
        xml = ET.parse(path.parent / "junit.xml")
        require(
            not xml.findall(".//failure")
            and not xml.findall(".//error")
            and not xml.findall(".//skipped"),
            "JUnit contains unsuccessful tests",
        )
        junit_nodes = [
            p.get("value")
            for p in xml.findall(".//testcase/properties/property")
            if p.get("name") == "ci_nodeid"
        ]
        require(
            junit_nodes == report["selected"], "JUnit does not match selected tests"
        )
        data = CoverageData(basename=str(path.parent / "coverage.data"))
        data.read()
        require(
            data.has_arcs() and bool(data.measured_files()), "Missing branch coverage"
        )
        measured = [f.replace("\\", "/") for f in data.measured_files()]
        require(
            all(f.startswith("app/") and ".." not in f.split("/") for f in measured),
            "Unexpected coverage source paths",
        )
        data_paths.append(str(path.parent / "coverage.data"))
    output.mkdir(parents=True, exist_ok=True)
    cov = Coverage(
        data_file=str(output / "coverage.data"), config_file="pyproject.toml"
    )
    cov.set_option("run:relative_files", True)
    cov.combine(data_paths=data_paths, strict=True, keep=True)
    cov.save()
    total = cov.report()
    cov.json_report(outfile=str(output / "coverage.json"))
    cov.xml_report(outfile=str(output / "coverage.xml"))
    require(total >= 70, "Combined branch coverage below 70%")
    result.update(identity=plan["identity"], coverage=total)
    write_json(output / "summary.json", result)
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("collect", "run", "plan", "aggregate"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--groups", type=int, default=4)
    parser.add_argument("--group", type=int, default=0)
    parser.add_argument("--dependency", default="")
    args = parser.parse_args()
    if args.mode in ("run", "collect"):
        return execute(args.mode, args.output, args.plan, args.group)
    if args.mode == "plan":
        report = read_json(args.input)
        require(
            report["exitcode"] == report["process_exitcode"] == 0, "Collection failed"
        )
        history = read_json(args.history) if args.history else {}
        weights = history.get(report["identity"]["python"], {})
        write_json(
            args.output,
            build_plan(report["inventory"], report["identity"], weights, args.groups),
        )
    else:
        aggregate(args.plan, args.input, args.output, args.dependency)
    return 0


if __name__ == "__main__":
    sys.exit(main())
