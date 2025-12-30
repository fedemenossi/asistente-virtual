from __future__ import annotations

from enum import Enum

from sqlalchemy import Enum as SqlEnum, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin


class TipoConsultorio(str, Enum):
    PRESENCIAL = "presencial"
    VIRTUAL = "virtual"


class Consultorio(Base, SoftDeleteMixin):
    __tablename__ = "consultorios"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    tipo: Mapped[TipoConsultorio] = mapped_column(
        SqlEnum(TipoConsultorio, native_enum=False), nullable=False
    )
    proveedor_turnos: Mapped[str | None] = mapped_column(String(100))
    configuracion_externa: Mapped[dict | None] = mapped_column(JSON)

    tenant = relationship("Tenant", back_populates="consultorios")
    turnos = relationship("Turno", back_populates="consultorio")
