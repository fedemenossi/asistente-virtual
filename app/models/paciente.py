from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class Paciente(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "pacientes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    telefono: Mapped[str] = mapped_column(String(32), nullable=False)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    apellido: Mapped[str] = mapped_column(String(100), nullable=False)
    dni: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    obra_social: Mapped[str | None] = mapped_column(String(100))
    insurance_number: Mapped[str | None] = mapped_column(String(100))
    fecha_nacimiento: Mapped[date | None] = mapped_column(Date, nullable=True)
    tipo_documento: Mapped[str | None] = mapped_column(String(50), nullable=True)
    numero_documento: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_number_normalized: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    financiador_seguro: Mapped[str | None] = mapped_column(String(200), nullable=True)
    genero: Mapped[str | None] = mapped_column(String(50), nullable=True)
    telefono_casa: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direccion: Mapped[str | None] = mapped_column(String(200), nullable=True)
    direccion_numero: Mapped[str | None] = mapped_column(String(40), nullable=True)
    departamento: Mapped[str | None] = mapped_column(String(40), nullable=True)
    piso: Mapped[str | None] = mapped_column(String(40), nullable=True)
    localidad: Mapped[str | None] = mapped_column(String(120), nullable=True)
    codigo_postal: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pais: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provincia: Mapped[str | None] = mapped_column(String(120), nullable=True)
    external_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    external_patient_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sync_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    tenant = relationship("Tenant", back_populates="pacientes")
    turnos = relationship("Turno", back_populates="paciente")
    payments = relationship("Payment", back_populates="patient")
