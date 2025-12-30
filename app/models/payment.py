from __future__ import annotations

from enum import Enum
from datetime import datetime

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utc_now


class PaymentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    patient_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), nullable=False)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("turnos.id"))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_payment_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[PaymentStatus] = mapped_column(
        SqlEnum(PaymentStatus, native_enum=False), default=PaymentStatus.PENDING
    )
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="ARS")
    description: Mapped[str | None] = mapped_column(String(255))
    payment_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )

    tenant = relationship("Tenant", back_populates="payments")
    patient = relationship("Paciente", back_populates="payments")
    appointment = relationship("Turno", back_populates="payments")
    subscription = relationship("Subscription", back_populates="payments")
    events = relationship("PaymentEvent", back_populates="payment")
