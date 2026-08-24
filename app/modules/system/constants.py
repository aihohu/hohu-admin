"""System module permission constants shared by services and seed scripts."""

DEPT_MOVE_PERMISSION = "system:dept:move"
"""Independent permission required to change department hierarchy."""

USER_ROLE_AUTH_PERMISSION = "system:user:role-auth"
"""Independent permission required to delegate user role membership."""

PHASE3_DESTRUCTIVE_PERMISSIONS = frozenset(
    {
        "system:dept:batch-delete",
        "system:dept:delete",
        "system:role:batch-delete",
        "system:role:delete",
    }
)
"""Original page permissions that R_SUPER must also hold for destructive writes."""
