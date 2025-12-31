from __future__ import annotations

from sqlalchemy import Boolean, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class Tenant(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    whatsapp_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    payment_settings: Mapped[dict | None] = mapped_column(JSON)
    calendar_settings: Mapped[dict | None] = mapped_column(JSON)

    consultorios = relationship("Consultorio", back_populates="tenant")
    pacientes = relationship("Paciente", back_populates="tenant")
    payments = relationship("Payment", back_populates="tenant")
