"""Host-maintenance entry point; explicit IDs, frozen baselines, no execution.

Use ``python -m scripts.reconcile_ai_legacy --help``. Output contains no business
payload. A maintenance evidence digest is an audit reference, not a stop-writers
proof: annotation invokes the direct/Compose stop and database evidence controller.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine
from app.modules.ai.service.reconciliation_annotation import REASONS


async def run(args):
    try:
        if args.command == "inspect":
            result = await AuthorizationRuntime(engine).reconciliation.inspect(
                tenant_id=args.tenant_id,
                operator_id=args.operator_id,
                log_ids=tuple(sorted(set(args.log_id))),
            )
        else:
            from tools.ops import authorization_maintenance as ops  # noqa: PLC0415

            with args.manifest.open("rb") as source:
                raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("inventory exceeds budget")
            manifest = json.loads(raw)
            result = await ops.run_command(
                argparse.Namespace(
                    command="execute",
                    action="ai.reconcile",
                    payload=None,
                    inventory=args.inventory,
                    control_dir=args.control_dir,
                ),
                reconciliation={
                    "tenant_id": args.tenant_id,
                    "operator_id": args.operator_id,
                    "manifest": manifest,
                    "reason": args.reason,
                    "evidence_sha256": args.evidence_sha256,
                    "maintenance_sha256": args.maintenance_sha256,
                },
            )
            result = result["result"]
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
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
    annotate.add_argument("--inventory", type=Path, required=True)
    annotate.add_argument(
        "--control-dir", type=Path, default=Path(settings.AUTHORIZATION_CONTROL_DIR)
    )
    annotate.add_argument("--manifest", type=Path, required=True)
    annotate.add_argument("--reason", choices=sorted(REASONS), required=True)
    annotate.add_argument("--evidence-sha256", required=True)
    annotate.add_argument(
        "--maintenance-sha256", help="optional expected runtime stop-evidence digest"
    )
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
