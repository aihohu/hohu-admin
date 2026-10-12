"""Add trusted recovery email and deployment-global mail configuration."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d01010100101"
down_revision = "c926ae092601"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sys_user",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "sys_mail_config",
        sa.Column("config_id", sa.Integer(), primary_key=True),
        sa.Column("values", postgresql.JSONB(), nullable=False),
        sa.Column("password_ciphertext", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True)),
        sa.Column("last_test_status", sa.String(20), nullable=False),
        sa.CheckConstraint("config_id = 1", name="ck_mail_singleton"),
    )
    op.execute(
        sa.text(
            "INSERT INTO sys_mail_config (config_id, values, password_ciphertext, version, last_test_status) VALUES (1, '{}', '', 1, 'untested')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE sys_setting SET setting_value = 'false' WHERE setting_key = 'register_enabled'"
        )
    )
    # Every writer, including imports and direct SQL, invalidates historical trust.
    op.execute(
        sa.text("""CREATE FUNCTION invalidate_changed_user_email() RETURNS trigger AS $$
    BEGIN
      IF NEW.user_email IS DISTINCT FROM OLD.user_email THEN
        NEW.email_verified_at := NULL;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    )
    op.execute(
        sa.text("""CREATE TRIGGER trg_user_email_trust BEFORE UPDATE ON sys_user
        FOR EACH ROW EXECUTE FUNCTION invalidate_changed_user_email()""")
    )


def downgrade():
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_user_email_trust ON sys_user"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS invalidate_changed_user_email()"))
    op.drop_column("sys_user", "email_verified_at")
    op.drop_table("sys_mail_config")
