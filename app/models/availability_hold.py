from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AvailabilityHoldStatus(str, Enum):
    ACTIVE = "active"
    CONVERTED = "converted"
    RELEASED = "released"


class AvailabilityHold(Base, TimestampMixin):
    __tablename__ = "availability_holds"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "consultorio_id",
            "external_slot_id",
            name="uq_availability_hold_slot",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    consultorio_id: Mapped[int] = mapped_column(ForeignKey("consultorios.id"), nullable=False)
    patient_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), nullable=False)
    turno_id: Mapped[int] = mapped_column(ForeignKey("turnos.id"), nullable=False)
    conversation_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    external_slot_id: Mapped[str] = mapped_column(String(200), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[AvailabilityHoldStatus] = mapped_column(
        SqlEnum(AvailabilityHoldStatus, native_enum=False),
        default=AvailabilityHoldStatus.ACTIVE,
        nullable=False,
    )
