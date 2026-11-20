"""Trusted publication provenance, never a second copy of configuration values."""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ConfigPublication(Base):
    __tablename__ = "sys_config_publication"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_id", "config_id"),
            ("sys_config.tenant_id", "sys_config.config_id"),
            name="fk_config_publication_tenant_config",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "publication_version >= 1", name="ck_config_publication_version"
        ),
        CheckConstraint("published_by_user_id > 0", name="ck_config_publication_actor"),
        CheckConstraint(
            "content_digest ~ '^[0-9a-f]{64}$'", name="ck_config_publication_digest"
        ),
        CheckConstraint(
            "btrim(schema_revision) <> ''", name="ck_config_publication_schema"
        ),
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    config_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    schema_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    publication_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
