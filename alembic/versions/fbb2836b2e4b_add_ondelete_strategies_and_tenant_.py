"""add ondelete strategies and tenant index for marketplace tables

Revision ID: fbb2836b2e4b
Revises: 47538f0c11ae
Create Date: 2026-06-17 13:16:39.537348

Code review follow-up for the marketplace schema:

* Add explicit ``ondelete=`` to every FK in the marketplace models so that
  deleting an App cascades through versions / reviews / ratings /
  permissions / installs, while deleting the *referenced* User or current
  Version uses ``SET NULL`` (preserving the App/Review row instead of
  cascading deletion back).
* Add a composite ``ix_mk_app_tenant_status`` index on ``mk_app`` to support
  tenant-scoped listing queries.

PostgreSQL cannot change the ``ondelete`` clause of an existing FK in place,
so each update is a ``drop_constraint`` followed by ``create_foreign_key``
with the new clause. Constraint names are preserved to keep the DB identical
to a fresh install.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fbb2836b2e4b"
down_revision: Union[str, Sequence[str], None] = "47538f0c11ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Each tuple: (constraint_name, source_table, referent_table, local_cols,
#              remote_cols, ondelete)
_FK_RECREATIONS = [
    # mk_app
    (
        "mk_app_author_id_fkey",
        "mk_app",
        "sys_user",
        ["author_id"],
        ["user_id"],
        "SET NULL",
    ),
    (
        "fk_mk_app_current_version_id_mk_app_version",
        "mk_app",
        "mk_app_version",
        ["current_version_id"],
        ["id"],
        "SET NULL",
    ),
    # mk_app_version
    (
        "mk_app_version_app_id_fkey",
        "mk_app_version",
        "mk_app",
        ["app_id"],
        ["id"],
        "CASCADE",
    ),
    # mk_app_review
    (
        "mk_app_review_app_id_fkey",
        "mk_app_review",
        "mk_app",
        ["app_id"],
        ["id"],
        "CASCADE",
    ),
    (
        "mk_app_review_version_id_fkey",
        "mk_app_review",
        "mk_app_version",
        ["version_id"],
        ["id"],
        "CASCADE",
    ),
    (
        "mk_app_review_human_reviewer_id_fkey",
        "mk_app_review",
        "sys_user",
        ["human_reviewer_id"],
        ["user_id"],
        "SET NULL",
    ),
    # mk_app_permission
    (
        "mk_app_permission_app_id_fkey",
        "mk_app_permission",
        "mk_app",
        ["app_id"],
        ["id"],
        "CASCADE",
    ),
    # mk_app_rating
    (
        "mk_app_rating_app_id_fkey",
        "mk_app_rating",
        "mk_app",
        ["app_id"],
        ["id"],
        "CASCADE",
    ),
    (
        "mk_app_rating_user_id_fkey",
        "mk_app_rating",
        "sys_user",
        ["user_id"],
        ["user_id"],
        "CASCADE",
    ),
    # mk_tenant_app
    (
        "mk_tenant_app_app_id_fkey",
        "mk_tenant_app",
        "mk_app",
        ["app_id"],
        ["id"],
        "CASCADE",
    ),
]

_TENANT_INDEX = "ix_mk_app_tenant_status"


def upgrade() -> None:
    """Apply FK ondelete strategies and tenant index."""
    for name, src, ref, local, remote, ondelete in _FK_RECREATIONS:
        op.drop_constraint(name, src, type_="foreignkey")
        op.create_foreign_key(
            name,
            src,
            ref,
            local,
            remote,
            ondelete=ondelete,
        )

    op.create_index(
        _TENANT_INDEX,
        "mk_app",
        ["tenant_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Revert FK ondelete strategies (back to default RESTRICT) and drop index."""
    op.drop_index(_TENANT_INDEX, table_name="mk_app")

    # Reverse order to mirror upgrade (avoids issues with cross-table refs).
    for name, src, ref, local, remote, _ondelete in reversed(_FK_RECREATIONS):
        op.drop_constraint(name, src, type_="foreignkey")
        op.create_foreign_key(name, src, ref, local, remote)
