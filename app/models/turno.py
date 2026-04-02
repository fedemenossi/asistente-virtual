from __future__ import annotations

from enum import Enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class TipoTurno(str, Enum):
    PRESENCIAL = "presencial"
    VIRTUAL = "virtual"


class EstadoTurno(str, Enum):
    DRAFT = "draft"
    PENDIENTE = "pendiente"
    WAITING_PAYMENT = "waiting_payment"
    CONFIRMADO = "confirmado"
    PAYMENT_FAILED = "payment_failed"
    CANCELADO = "cancelado"
    COMPLETED = "completed"


class AppointmentStatus(str, Enum):
    DRAFT = "draft"
    WAITING_PAYMENT = "waiting_payment"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Turno(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "turnos"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), nullable=False)
    consultorio_id: Mapped[int] = mapped_column(
        ForeignKey("consultorios.id"), nullable=False
    )
    fecha_hora: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime)
    end_at: Mapped[datetime | None] = mapped_column(DateTime)
    timezone: Mapped[str | None] = mapped_column(String(64))
    tipo: Mapped[TipoTurno] = mapped_column(SqlEnum(TipoTurno, native_enum=False))
    provider: Mapped[str | None] = mapped_column(String(50))
    external_id: Mapped[str | None] = mapped_column(String(200))
    external_status: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    reminder_24h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_2h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancellation_reason: Mapped[str | None] = mapped_column(String(255))
    origen_externo: Mapped[str | None] = mapped_column(String(100))
    referencia_externa: Mapped[str | None] = mapped_column(String(100))
    external_calendar_provider: Mapped[str | None] = mapped_column(String(50))
    external_calendar_id: Mapped[str | None] = mapped_column(String(200))
    external_event_id: Mapped[str | None] = mapped_column(String(200))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    estado: Mapped[EstadoTurno] = mapped_column(
        SqlEnum(EstadoTurno, native_enum=False), default=EstadoTurno.PENDIENTE
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        SqlEnum(AppointmentStatus, native_enum=False), default=AppointmentStatus.DRAFT
    )

    tenant = relationship("Tenant", back_populates="turnos")
    paciente = relationship("Paciente", back_populates="turnos")
    consultorio = relationship("Consultorio", back_populates="turnos")
    payments = relationship("Payment", back_populates="appointment")
