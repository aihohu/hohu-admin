"""Pure, conservative legacy candidates; not identity or authorization proofs.

The future trusted adapter supplies each role's own explicit permissions and
row scope. Menu visibility does not revoke linked permission codes. Neither
R_SUPER nor another role contributes implicit fields. No IO or activation.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from app.core.exceptions import InvalidParameterException
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.evaluator import FieldGrant

_TEMPLATE_REVISION = "legacy-user-fields/v1"
_MAX_SOURCE_ITEMS = 1024
_MAX_SOURCE_BYTES = 65536
_MAX_ID = 2**63 - 1
_PERMISSION = re.compile(r"[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*")
_PROFILE_FIELDS = (
    "user_name",
    "nickname",
    "user_phone",
    "user_email",
    "user_gender",
    "status",
)
_LIST_FIELDS = (*_PROFILE_FIELDS, "create_time", "roles", "departments")
_MASKS = {"user_phone": "phone/v1", "user_email": "email/v1"}


@dataclass(frozen=True, slots=True)
class RoleBaselineSource:
    """A single role snapshot, supplied by a future tenant-scoped adapter."""

    tenant_id: int
    role_id: int
    role_code: str
    status: str
    permissions: frozenset[str]
    data_scope: str
    dept_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BaselineCandidate:
    rules: tuple[FieldGrant, ...]
    source_digest: str
    catalog_digest: str
    review_notes: tuple[str, ...]
    template_revision: str = _TEMPLATE_REVISION


def _invalid() -> InvalidParameterException:
    return InvalidParameterException(
        "用户字段候选来源无效或超出预算", error_code="FIELD_POLICY_INVALID"
    )


def _valid_id(value: object, *, allow_zero: bool = False) -> bool:
    return type(value) is int and (0 if allow_zero else 1) <= value <= _MAX_ID


def _source_bytes(source: RoleBaselineSource) -> bytes:
    if not isinstance(source, RoleBaselineSource):
        raise _invalid()
    if (
        not _valid_id(source.tenant_id, allow_zero=True)
        or not _valid_id(source.role_id)
        or not isinstance(source.role_code, str)
        or not 1 <= len(source.role_code) <= 50
        or any(char.isspace() or not char.isprintable() for char in source.role_code)
        or source.status not in ("1", "2")
        or source.data_scope not in ("1", "2", "3", "4", "5")
        or not isinstance(source.permissions, frozenset)
        or len(source.permissions) > _MAX_SOURCE_ITEMS
        or not isinstance(source.dept_ids, tuple)
        or len(source.dept_ids) > _MAX_SOURCE_ITEMS
    ):
        raise _invalid()
    if any(
        not isinstance(permission, str)
        or len(permission) > 128
        or _PERMISSION.fullmatch(permission) is None
        for permission in source.permissions
    ):
        raise _invalid()
    if any(not _valid_id(value) for value in source.dept_ids):
        raise _invalid()
    if tuple(sorted(set(source.dept_ids))) != source.dept_ids:
        raise _invalid()
    encoded = json.dumps(
        {
            "tenant_id": source.tenant_id,
            "role_id": source.role_id,
            "role_code": source.role_code,
            "status": source.status,
            "permissions": sorted(source.permissions),
            "data_scope": source.data_scope,
            "dept_ids": source.dept_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_SOURCE_BYTES:
        raise _invalid()
    return encoded


def build_role_baseline(source: RoleBaselineSource) -> BaselineCandidate:
    """Enumerate reviewed existing capabilities, without widening row scope.

    Rules remain candidates until explicitly reviewed. Runtime entrypoint caps,
    functional authorization, delegation and record applicability are separate.
    New catalog fields never enter this fixed template automatically.
    """
    source_digest = hashlib.sha256(_source_bytes(source)).hexdigest()
    rules: dict[tuple[str, str, str], FieldGrant] = {}
    notes = {
        "候选未激活；来源摘要不是授权令牌，启用前须重新核对当前角色及行范围。",
        "本人自助授予、AI 专属聚合和未列出的入口能力需单独审核。",
    }

    def add(fields: tuple[str, ...], operation: str, purpose: str) -> None:
        for field in fields:
            masked = purpose == "management.read" and field in _MASKS
            rules[(field, purpose, operation)] = FieldGrant(
                role_id=str(source.role_id),
                field_id=field,
                operation=operation,
                purpose=purpose,
                read="masked" if masked else "plain",
                mask_id=_MASKS[field] if masked else None,
            )

    permissions = source.permissions if source.status == "1" else frozenset()
    if source.status != "1":
        notes.add("停用角色不生成授予；恢复角色时须重新审核候选。")
    if source.role_code == "R_SUPER":
        notes.add("R_SUPER 的 HTTP 功能旁路不转换为字段授予；仅映射显式功能码。")
    if "system:user:list" in permissions:
        add(_LIST_FIELDS, "read", "management.read")
        add(("user_name", "nickname"), "contains", "management.query")
        add(("user_gender", "status", "roles"), "eq", "management.query")
        notes.add("电话和邮箱仅生成 masked 读取；原值查询不自动迁移，须显式批准。")
    if "system:user:add" in permissions:
        add((*_PROFILE_FIELDS, "password"), "create", "management.create")
        notes.add("创建时默认关系及显式关系输入须保留领域授权，不自动授予关系替换。")
    if "system:user:edit" in permissions:
        add(_PROFILE_FIELDS, "update", "management.update")
        if "system:user:role-auth" in permissions:
            add(("roles",), "replace", "management.relations")
        if "system:dept:list" in permissions:
            add(("departments",), "replace", "management.relations")
    if "system:user:reset-password" in permissions:
        add(("password",), "update", "management.update")
    if "system:user:export" in permissions:
        add(_LIST_FIELDS, "export", "export")
        notes.add("导出与同记录 management.read 取低；不补读取授予或提升掩码等级。")
    if {"system:user:import", "system:dept:list"} <= permissions:
        add((*_PROFILE_FIELDS, "employee_no"), "create", "import")
        add((*_PROFILE_FIELDS, "employee_no"), "update", "import")
        add(("user_name", "employee_no"), "exact", "import")
        add(("departments",), "replace", "import")
        if "system:user:role-auth" in permissions:
            add(("roles",), "replace", "import")
        notes.add(
            "导入改名仍限工号匹配 FULL_SYNC；无密码输入，角色与部门继续受委派规则限制。"
        )
    elif "system:user:import" in permissions:
        notes.add("缺少该角色自身的部门列表功能码，不生成导入候选。")
    return BaselineCandidate(
        rules=tuple(rules[key] for key in sorted(rules)),
        source_digest=source_digest,
        catalog_digest=catalog_digest(),
        review_notes=tuple(sorted(notes)),
    )
