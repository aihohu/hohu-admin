"""Static, reviewed user-fields/1 catalog. No tenant-supplied expressions."""

from dataclasses import dataclass
from types import MappingProxyType

CATALOG_REVISION = "user-fields/1"


@dataclass(frozen=True)
class UserField:
    field_id: str
    readable: bool = True
    writable: frozenset[str] = frozenset()
    query_ops: frozenset[str] = frozenset()
    mask_id: str | None = None
    read_purposes: frozenset[str] | None = None
    write_purposes: frozenset[str] | None = None
    query_purposes: frozenset[str] | None = None


_PROFILE_WRITE = frozenset({"create", "update"})
_TEXT_QUERY = frozenset({"exact", "contains"})
_ENUM_QUERY = frozenset({"eq", "group", "distinct"})

USER_FIELDS = MappingProxyType(
    {
        field.field_id: field
        for field in (
            UserField("user_id"),
            UserField("user_name", writable=_PROFILE_WRITE, query_ops=_TEXT_QUERY),
            UserField("nickname", writable=_PROFILE_WRITE, query_ops=_TEXT_QUERY),
            UserField(
                "user_phone",
                writable=_PROFILE_WRITE,
                query_ops=_TEXT_QUERY,
                mask_id="phone/v1",
            ),
            UserField(
                "user_email",
                writable=_PROFILE_WRITE,
                query_ops=_TEXT_QUERY,
                mask_id="email/v1",
            ),
            UserField("user_gender", writable=_PROFILE_WRITE, query_ops=_ENUM_QUERY),
            UserField("status", writable=_PROFILE_WRITE, query_ops=_ENUM_QUERY),
            UserField(
                "user_avatar",
                writable=frozenset({"update"}),
                write_purposes=frozenset({"own-account"}),
            ),
            UserField(
                "employee_no",
                writable=_PROFILE_WRITE,
                query_ops=frozenset({"exact"}),
                write_purposes=frozenset({"import"}),
                query_purposes=frozenset({"import"}),
            ),
            UserField(
                "roles", writable=frozenset({"replace"}), query_ops=frozenset({"eq"})
            ),
            UserField(
                "departments",
                writable=frozenset({"replace"}),
                query_ops=frozenset({"eq"}),
            ),
            UserField("create_time"),
            UserField("update_time"),
            UserField("password", readable=False, writable=_PROFILE_WRITE),
            UserField("email_verified", read_purposes=frozenset({"own-account"})),
            UserField("hashed_password", readable=False),
            UserField("auth_version", readable=False),
            UserField("tenant_id", readable=False),
            UserField("email_verified_at", readable=False),
        )
    }
)
