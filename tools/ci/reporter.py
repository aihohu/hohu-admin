"""Record full collection, main test phases and subtests separately."""

import os
import time
from collections import defaultdict
from pathlib import Path

import pytest

from tools.ci.sharding import identity, read_json, require, write_json


def pytest_configure(config):
    config._ci_report = {
        "identity": identity(),
        "inventory": [],
        "selected": [],
        "group": int(os.environ["HOHU_CI_GROUP"]),
        "phases": [],
        "subtests": [],
        "started": time.time(),
    }


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    report = config._ci_report
    report["inventory"] = [item.nodeid for item in items]
    if path := os.environ.get("HOHU_CI_PLAN"):
        plan = read_json(Path(path))
        require(plan["identity"] == report["identity"], "Worker identity mismatch")
        require(plan["inventory"] == report["inventory"], "Worker collection mismatch")
        selected = set(plan["groups"][report["group"]])
        removed = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        config.hook.pytest_deselected(items=removed)
    report["selected"] = [item.nodeid for item in items]
    for item in items:
        item.user_properties.append(("ci_nodeid", item.nodeid))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    # Store only this invocation's config; do not import or modify app fixtures.
    global _active
    _active = item.config._ci_report
    return (yield)


def pytest_runtest_logreport(report):
    record = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "seconds": report.duration,
    }
    _active["subtests" if hasattr(report, "context") else "phases"].append(record)


def pytest_sessionfinish(session, exitstatus):
    report = session.config._ci_report
    report.update(exitcode=int(exitstatus), finished=time.time())
    files = defaultdict(float)
    for phase in report["phases"]:
        files[phase["nodeid"].split("::", 1)[0]] += phase["seconds"]
    report["file_seconds"] = dict(sorted(files.items(), key=lambda pair: -pair[1]))
    output = Path(os.environ["HOHU_CI_OUTPUT"])
    write_json(output / "report.json", report)
