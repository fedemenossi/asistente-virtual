from __future__ import annotations

from sqlalchemy import ForeignKey, String
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

    tenant = relationship("Tenant", back_populates="pacientes")
    turnos = relationship("Turno", back_populates="paciente")
