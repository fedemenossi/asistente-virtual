from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class ArcaInvoiceEvent(Base):
    __tablename__ = "arca_invoice_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("billing_invoices.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
