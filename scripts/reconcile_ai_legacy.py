"""Host-maintenance entry point; explicit IDs, frozen baselines, no execution.

Use ``python -m scripts.reconcile_ai_legacy --help``. Output contains no business
payload. A maintenance evidence digest is an audit reference, not a stop-writers
proof: the operator must independently stop old writers before applying.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.service.legacy_reconciliation_service import (
    legacy_reconciliation_service,
)
from app.modules.ai.service.reconciliation_annotation import REASONS


async def run(args):
    tenant = TenantContext(
        tenant_id=args.tenant_id,
        tenant_code="maintenance",
        actor_user_id=args.operator_id,
        tenant_version=1,
        source="access_token",
    )
    try:
        async with AsyncSessionLocal() as db, db.begin():
            if args.command == "inspect":
                if not 1 <= len(set(args.log_id)) <= 100:
                    raise ValueError("select between 1 and 100 explicit log IDs")
                result = [
                    await legacy_reconciliation_service.inspect(
                        db, tenant=tenant, log_id=log_id
                    )
                    for log_id in sorted(set(args.log_id))
                ]
            else:
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
                if not isinstance(manifest, list) or not 1 <= len(manifest) <= 100:
                    raise ValueError("invalid frozen inventory")
                ids = [int(item["logId"]) for item in manifest]
                if len(ids) != len(set(ids)) or any(
                    item["tenantId"] != str(args.tenant_id) for item in manifest
                ):
                    raise ValueError("inventory tenant or IDs mismatch")
                await legacy_reconciliation_service.lock_inventory(
                    db, tenant=tenant, manifest=manifest
                )
                manifest.sort(
                    key=lambda item: (
                        int(item["conversationId"]),
                        int(item["sourceMessageId"] or 0),
                        int(item["logId"]),
                    )
                )
                result = [
                    await legacy_reconciliation_service.annotate(
                        db,
                        tenant=tenant,
                        log_id=int(item["logId"]),
                        baseline=item["baseline"],
                        reason=args.reason,
                        evidence_sha256=args.evidence_sha256,
                        maintenance_sha256=args.maintenance_sha256,
                    )
                    for item in manifest
                ]
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--operator-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="read only, exact log IDs")
    inspect.add_argument("--log-id", type=int, action="append", required=True)
    annotate = commands.add_parser("annotate", help="append unverified evidence only")
    annotate.add_argument("--manifest", type=Path, required=True)
    annotate.add_argument("--reason", choices=sorted(REASONS), required=True)
    annotate.add_argument("--evidence-sha256", required=True)
    annotate.add_argument("--maintenance-sha256", required=True)
    args = parser.parse_args()
    # Never overwrite the frozen inventory or another existing operator artifact.
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("output must be a new file in an existing protected directory")
    try:
        asyncio.run(run(args))
    except (BusinessException, ValueError, KeyError, TypeError):
        parser.exit(
            1, "Reconciliation refused; re-inspect bindings, authority and inventory.\n"
        )


if __name__ == "__main__":
    main()
