from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ArcaBillableItem(Base, TimestampMixin):
    __tablename__ = "billing_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_arca_billable_item_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    tax_rate: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    iva_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="PES")
    concepto: Mapped[int] = mapped_column(nullable=False, default=2)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_item: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
