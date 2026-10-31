"""User identity copies require an original object, field and purpose.

This provenance is internal metadata, not an authorization capability. Current
record scope and decisions must be reevaluated under the caller's G/B barrier.
Historical free text without this evidence is hidden as a whole.
"""

from dataclasses import dataclass

from app.core.exceptions import InvalidParameterException
from app.modules.system.field_policy.evaluator import project_user_values

_IDENTITY_FIELDS = frozenset(
    {"user_name", "nickname", "user_phone", "user_email", "user_avatar"}
)


@dataclass(frozen=True, slots=True)
class UserFieldSource:
    tenant_id: int
    user_id: int
    field_id: str
    purpose: str

    def __post_init__(self):
        if (
            type(self.tenant_id) is not int
            or not 0 <= self.tenant_id < 2**63
            or type(self.user_id) is not int
            or not 0 < self.user_id < 2**63
            or self.field_id not in _IDENTITY_FIELDS
            or self.purpose not in {"management.read", "own-account"}
        ):
            raise InvalidParameterException(
                "用户字段来源无效", error_code="FIELD_POLICY_INVALID"
            )


@dataclass(frozen=True, slots=True)
class IdentityCopy:
    visible: bool = False
    value: str | None = None


def project_identity_copy(*, source, current_identity, current_values, decisions):
    if (
        type(source) is not UserFieldSource
        or type(current_identity) is not tuple
        or any(type(part) is not int for part in current_identity)
        or current_identity != (source.tenant_id, source.user_id)
        or decisions is None
        or decisions.purpose != source.purpose
        or source.field_id not in current_values
    ):
        return IdentityCopy()
    value = current_values[source.field_id]
    if value is not None and not isinstance(value, str):
        return IdentityCopy()
    projection = project_user_values(
        raw_values={source.field_id: value},
        decisions=decisions,
        entrypoint_caps={
            source.field_id: "masked"
            if source.field_id in {"user_phone", "user_email"}
            else "plain"
        },
    )
    if source.field_id not in projection.values:
        return IdentityCopy()
    return IdentityCopy(True, projection.values[source.field_id])
