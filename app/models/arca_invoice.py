from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import Date, DateTime, Enum as SqlEnum, ForeignKey, JSON, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ArcaInvoiceStatus(str, Enum):
    DRAFT = "draft"
    PENDING_AUTHORIZATION = "pending_authorization"
    AUTHORIZED = "authorized"
    REJECTED = "rejected"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    CANCELLED = "cancelled"


class ArcaInvoice(Base, TimestampMixin):
    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "environment",
            "represented_cuit",
            "pto_vta",
            "cbte_tipo",
            "cbte_nro",
            name="uq_arca_invoice_number_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "external_consultation_id",
            name="uq_arca_invoice_external_consultation",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("pacientes.id"), nullable=True)
    fiscal_contact_id: Mapped[int | None] = mapped_column(ForeignKey("billing_fiscal_contacts.id"), nullable=True)
    external_consultation_id: Mapped[int | None] = mapped_column(ForeignKey("billing_external_consultations.id"), nullable=True, index=True)
    billing_item_id: Mapped[int | None] = mapped_column(ForeignKey("billing_items.id"), nullable=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("turnos.id"), nullable=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(30), nullable=False, default="consultation")
    receiver_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    receiver_iva_condition_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    service_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    sale_condition: Mapped[str | None] = mapped_column(String(80), nullable=True)
    represented_cuit: Mapped[str] = mapped_column(String(11), nullable=False)
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="homo")
    pto_vta: Mapped[int] = mapped_column(nullable=False)
    cbte_tipo: Mapped[int] = mapped_column(nullable=False)
    cbte_nro: Mapped[int | None] = mapped_column(nullable=True)
    concepto: Mapped[int | None] = mapped_column(nullable=True)
    doc_tipo: Mapped[int | None] = mapped_column(nullable=True)
    doc_nro: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cbte_fch: Mapped[date | None] = mapped_column(Date, nullable=True)
    imp_total: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    imp_tot_conc: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    imp_neto: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    imp_op_ex: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    imp_trib: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    imp_iva: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    mon_id: Mapped[str] = mapped_column(String(3), nullable=False, default="PES")
    mon_cotiz: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    status: Mapped[ArcaInvoiceStatus] = mapped_column(
        SqlEnum(ArcaInvoiceStatus, native_enum=False),
        nullable=False,
        default=ArcaInvoiceStatus.DRAFT,
    )
    cae: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cae_fch_vto: Mapped[date | None] = mapped_column(Date, nullable=True)
    diagnosis_original_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_final_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_email: Mapped[bool | None] = mapped_column(nullable=True)
    email_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    document_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    document_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    document_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    qr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pdf_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pdf_generated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    request_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
