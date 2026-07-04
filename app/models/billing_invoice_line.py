from __future__ import annotations

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BillingInvoiceLine(Base, TimestampMixin):
    __tablename__ = "billing_invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("billing_invoices.id"), nullable=False, index=True)
    item_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis_text: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    tax_rate: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
