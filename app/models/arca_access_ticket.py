from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ArcaAccessTicket(Base, TimestampMixin):
    __tablename__ = "arca_auth_tokens"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "represented_cuit",
            "environment",
            "service",
            name="uq_arca_access_ticket_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    represented_cuit: Mapped[str] = mapped_column(String(11), nullable=False)
    environment: Mapped[str] = mapped_column(String(10), nullable=False)
    service: Mapped[str] = mapped_column(String(30), nullable=False, default="wsfe")
    token_encrypted: Mapped[str] = mapped_column(String(5000), nullable=False)
    sign_encrypted: Mapped[str] = mapped_column(String(5000), nullable=False)
    expiration_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
