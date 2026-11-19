"""Record full collection, main test phases and subtests separately."""

import os
import time
from collections import defaultdict
from pathlib import Path

import pytest

from tools.ci.sharding import identity, read_json, require, write_json


def case_key(item):
    """Binary payload reprs can contain ZIP timestamps; bind their source slots.

    Keep pytest's execution and original nodeid untouched. Every parameter index
    is included, so separate binary cases of one function remain distinct.
    """
    callspec = getattr(item, "callspec", None)
    if callspec and any(
        isinstance(value, (bytes, bytearray)) for value in callspec.params.values()
    ):
        slots = ",".join(
            f"{name}:{index}" for name, index in sorted(callspec.indices.items())
        )
        return f"{item.parent.nodeid}::{item.originalname}[@ci-binary:{slots}]"
    return item.nodeid


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
    config._ci_keys = {item.nodeid: case_key(item) for item in items}
    report["inventory"] = [config._ci_keys[item.nodeid] for item in items]
    require(len(set(report["inventory"])) == len(items), "Duplicate case identity")
    report["raw_nodeids"] = {key: raw for raw, key in config._ci_keys.items()}
    if path := os.environ.get("HOHU_CI_PLAN"):
        plan = read_json(Path(path))
        require(plan["identity"] == report["identity"], "Worker identity mismatch")
        require(plan["inventory"] == report["inventory"], "Worker collection mismatch")
        selected = set(plan["groups"][report["group"]])
        removed = [
            item for item in items if config._ci_keys[item.nodeid] not in selected
        ]
        items[:] = [item for item in items if config._ci_keys[item.nodeid] in selected]
        config.hook.pytest_deselected(items=removed)
    report["selected"] = [config._ci_keys[item.nodeid] for item in items]
    for item in items:
        item.user_properties.append(("ci_nodeid", config._ci_keys[item.nodeid]))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    # Store only this invocation's config; do not import or modify app fixtures.
    global _active, _keys
    _active = item.config._ci_report
    _keys = item.config._ci_keys
    return (yield)


def pytest_runtest_logreport(report):
    record = {
        "nodeid": _keys[report.nodeid],
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
