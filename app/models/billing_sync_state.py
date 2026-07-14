from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BillingSyncState(Base, TimestampMixin):
    __tablename__ = "billing_sync_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "consultorio_id", "source", name="uq_billing_sync_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    consultorio_id: Mapped[int] = mapped_column(ForeignKey("consultorios.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="consultorio_movil")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
