from __future__ import annotations

from enum import Enum
from datetime import datetime

from sqlalchemy import Enum as SqlEnum, ForeignKey, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin


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


class Turno(Base, SoftDeleteMixin):
    __tablename__ = "turnos"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), nullable=False)
    consultorio_id: Mapped[int] = mapped_column(
        ForeignKey("consultorios.id"), nullable=False
    )
    fecha_hora: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime)
    end_at: Mapped[datetime | None] = mapped_column(DateTime)
    timezone: Mapped[str | None] = mapped_column(String(64))
    tipo: Mapped[TipoTurno] = mapped_column(SqlEnum(TipoTurno, native_enum=False))
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

    paciente = relationship("Paciente", back_populates="turnos")
    consultorio = relationship("Consultorio", back_populates="turnos")
    payments = relationship("Payment", back_populates="appointment")
