from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PatientCreditStatus(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class PatientCredit(Base, TimestampMixin):
    __tablename__ = "patient_credits"
    __table_args__ = (
        UniqueConstraint("source_payment_id", name="uq_patient_credit_source_payment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), nullable=False, index=True)
    source_payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[PatientCreditStatus] = mapped_column(
        SqlEnum(PatientCreditStatus, native_enum=False),
        default=PatientCreditStatus.ACTIVE,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    consumed_turno_id: Mapped[int | None] = mapped_column(ForeignKey("turnos.id"))
