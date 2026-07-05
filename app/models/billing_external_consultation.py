from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BillingExternalConsultation(Base, TimestampMixin):
    __tablename__ = "billing_external_consultations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "external_provider",
            "external_id",
            name="uq_billing_external_consultation",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    consultorio_id: Mapped[int | None] = mapped_column(ForeignKey("consultorios.id"), nullable=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("pacientes.id"), nullable=True, index=True)
    arca_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("billing_invoices.id"), nullable=True, index=True)
    billing_item_id: Mapped[int | None] = mapped_column(ForeignKey("billing_items.id"), nullable=True)
    external_provider: Mapped[str] = mapped_column(String(50), nullable=False, default="consultorio_movil")
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    external_staff_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    attended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    patient_external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    patient_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    patient_document: Mapped[str | None] = mapped_column(String(40), nullable=True)
    patient_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    patient_phone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    insurance_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    professional_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    practice_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    diagnosis_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    selected_for_billing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    send_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    billed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
