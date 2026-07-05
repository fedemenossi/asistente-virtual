from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BillingSetting(Base, TimestampMixin):
    __tablename__ = "billing_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="homo")
    cuit_emisor: Mapped[str | None] = mapped_column(String(11), nullable=True)
    punto_venta: Mapped[int | None] = mapped_column(nullable=True)
    cbte_tipo_default: Mapped[int | None] = mapped_column(nullable=True)
    concepto_default: Mapped[int | None] = mapped_column(nullable=True)
    moneda_default: Mapped[str] = mapped_column(String(3), nullable=False, default="PES")
    condicion_iva_emisor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    condicion_iva_receptor_default: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cert_pem_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key_pem_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    cert_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arca_service: Mapped[str] = mapped_column(String(30), nullable=False, default="wsfe")
    email_invoice_enabled_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_subject_template: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_body_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    diagnosis_visible_on_invoice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    include_diagnosis_in_invoice_pdf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
