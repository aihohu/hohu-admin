"""Deployment-global SMTP configuration; never inherited from a tenant."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MailConfig(Base):
    __tablename__ = "sys_mail_config"
    __table_args__ = (CheckConstraint("config_id = 1", name="ck_mail_singleton"),)

    config_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    values: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    password_ciphertext: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_status: Mapped[str] = mapped_column(String(20), default="untested")
