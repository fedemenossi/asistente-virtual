from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ArcaFiscalSeries(Base, TimestampMixin):
    __tablename__ = "arca_fiscal_series"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "represented_cuit",
            "environment",
            "pto_vta",
            "cbte_tipo",
            name="uq_arca_fiscal_series_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    represented_cuit: Mapped[str] = mapped_column(String(11), nullable=False)
    environment: Mapped[str] = mapped_column(String(10), nullable=False)
    pto_vta: Mapped[int] = mapped_column(nullable=False)
    cbte_tipo: Mapped[int] = mapped_column(nullable=False)
