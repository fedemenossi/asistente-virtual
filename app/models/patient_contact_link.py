from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class PatientContactLinkStatus(str, Enum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


class PatientContactLink(Base, TimestampMixin):
    __tablename__ = "patient_contact_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "contact_id",
            "patient_id",
            name="uq_patient_contact_link_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("whatsapp_contacts.id"), nullable=False, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=PatientContactLinkStatus.APPROVED.value)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    contact = relationship("WhatsappContact", back_populates="patient_links")
    patient = relationship("Paciente", back_populates="contact_links")
