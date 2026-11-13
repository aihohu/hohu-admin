"""Complete exact-key setting facts without fetching hidden plaintext."""

import hashlib
import hmac
import json

from sqlalchemy import LargeBinary, case, func, select

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.system.models.setting import SystemSetting
from app.modules.system.service.authorization_read_budget import read_phase
from app.modules.system.settings_catalog import SETTING_GROUPS, SETTINGS


@read_phase("projection")
async def setting_snapshot(db, tenant_id, group, *, readable=()):
    definitions = {key: d for key, d in SETTINGS.items() if d.group == group}
    if group not in SETTING_GROUPS:
        raise BusinessRuleException(
            "Unknown settings group", error_code="SETTING_GROUP_INVALID"
        )
    table = SystemSetting.__table__
    maximum = 400 * 1024 if group == "agreements" else 8192
    raw_digest = func.pg_catalog.sha256(
        func.pg_catalog.convert_to(table.c.setting_value, "UTF8", type_=LargeBinary()),
        type_=LargeBinary(),
    ).label("value_digest")
    value = case(
        (
            (table.c.setting_key.in_(tuple(readable)))
            & (func.pg_catalog.octet_length(table.c.setting_value) <= maximum),
            table.c.setting_value,
        ),
        else_=None,
    ).label("setting_value")
    rows = (
        (
            await db.execute(
                select(
                    table.c.setting_id,
                    table.c.setting_key,
                    table.c.status,
                    raw_digest,
                    value,
                )
                .where(
                    table.c.tenant_id == tenant_id,
                    table.c.setting_key.in_(tuple(definitions)),
                )
                .order_by(table.c.setting_key)
                .limit(len(definitions) + 1)
            )
        )
        .mappings()
        .all()
    )
    facts = [
        (
            row["setting_id"],
            row["setting_key"],
            row["status"],
            row["value_digest"].hex(),
        )
        for row in rows
    ]
    revision = hmac.new(
        settings.SECRET_KEY.encode(),
        json.dumps(
            [
                tenant_id,
                group,
                {key: d.default for key, d in definitions.items()},
                facts,
            ],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    return rows, revision
