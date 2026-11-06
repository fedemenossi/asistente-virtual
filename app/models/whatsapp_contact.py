from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class WhatsappContact(Base, TimestampMixin):
    __tablename__ = "whatsapp_contacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_normalized", name="uq_whatsapp_contact_tenant_phone"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    phone_normalized: Mapped[str] = mapped_column(String(32), nullable=False)

    tenant = relationship("Tenant", back_populates="whatsapp_contacts")
    patient_links = relationship("PatientContactLink", back_populates="contact")
