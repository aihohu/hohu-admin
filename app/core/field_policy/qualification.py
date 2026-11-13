"""Closed qualification evidence loaded by a trusted owner, outside database locks."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.field_policy.contracts import require
from app.core.field_policy.documents import catalog_parts, digest, read_json

CHECKS = frozenset(
    {
        "two_tenants",
        "all_writers",
        "query_scope",
        "source_closure",
        "old_client_guard",
        "direct_stop",
        "compose_stop",
        "database_quiescence",
        "upgrade_restore",
        "independent_review",
    }
)


@dataclass(frozen=True, slots=True)
class Qualification:
    build_sha: str
    catalog_digest: str
    action_digest: str
    adapter_digest: str
    evidence_digest: str
    expires_at: datetime

    def verify(self, catalog, build_sha):
        require(
            (
                self.build_sha,
                self.catalog_digest,
                self.action_digest,
                self.adapter_digest,
            )
            == (build_sha, catalog.digest, *catalog_parts(catalog))
        )
        require(self.expires_at > datetime.now(UTC))


def read_qualification(path, *, catalog, build_sha):
    """The path is server configuration, never a request body or offline draft."""
    require(
        type(build_sha) is str and re.fullmatch(r"[0-9a-f]{40}", build_sha) is not None
    )
    with path.open("rb") as source:
        encoded = source.read(1024 * 1024 + 1)
    require(len(encoded) <= 1024 * 1024)
    doc = read_json(encoded)
    require(
        type(doc) is dict
        and set(doc)
        == {
            "schema_version",
            "build_sha",
            "database_revision",
            "catalog_digest",
            "action_digest",
            "adapter_digest",
            "objects",
            "checks",
            "expires_at",
            "evidence",
        }
    )
    require(
        type(doc["schema_version"]) is int
        and doc["schema_version"] == 2
        and doc["database_revision"] == "e020f3a00001"
    )
    require(type(doc["objects"]) is list and doc["objects"] == sorted(catalog.objects))
    require(
        type(doc["checks"]) is dict
        and set(doc["checks"]) == CHECKS
        and all(v is True for v in doc["checks"].values())
    )
    # Check results and immutable report identities are separate; neither is inferred from a build number.
    require(type(doc["evidence"]) is dict and set(doc["evidence"]) == CHECKS)
    require(
        all(
            type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v)
            for v in doc["evidence"].values()
        )
    )
    require(type(doc["expires_at"]) is str)
    try:
        expires = datetime.fromisoformat(doc["expires_at"])
    except ValueError:
        require(False)
    require(
        expires.tzinfo is not None
        and 0 < (expires - datetime.now(UTC)).total_seconds() <= 86400
    )
    proof = Qualification(
        doc["build_sha"],
        doc["catalog_digest"],
        doc["action_digest"],
        doc["adapter_digest"],
        digest(doc),
        expires,
    )
    proof.verify(catalog, build_sha)
    return proof
