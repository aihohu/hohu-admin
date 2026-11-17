"""Consume one stopped maintenance cycle without losing its protected records."""

import hashlib
import os
from dataclasses import dataclass

from app.core.authorization_maintenance import _required

NAMES = ("policy-prepare.json", "install.json", "stop.json")


def bounded_bytes(path):
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise _required()
    return raw


@dataclass(frozen=True)
class ControlArchive:
    records: tuple

    def consume(self):
        # stop is last, so normal process admission stays closed until completion.
        for source, _, original in self.records:
            if bounded_bytes(source) != original:
                raise _required()
            source.unlink()

    def restore(self):
        # Recreate the ingress fence first and never overwrite a different fact.
        for source, archived, original in reversed(self.records):
            if bounded_bytes(archived) != original:
                raise _required()
            if source.exists():
                if bounded_bytes(source) != original:
                    raise _required()
            else:
                os.link(archived, source)


def archive_control_records(directory):
    stop = bounded_bytes(directory / "stop.json")
    identity = hashlib.sha256(stop).hexdigest()
    archive = directory / "completed" / identity
    archive.mkdir(parents=True, exist_ok=True)
    records = []
    for name in NAMES:
        source = directory / name
        if name != "stop.json" and not source.exists():
            continue
        raw = bounded_bytes(source)
        archived = archive / name
        if not archived.exists():
            with archived.open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        if bounded_bytes(archived) != raw:
            raise _required()
        records.append((source, archived, raw))
    return ControlArchive(tuple(records))
