from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class EstadoConversacion(Base):
    __tablename__ = "estados_conversacion"

    telefono: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    estado_actual: Mapped[str] = mapped_column(String(50), nullable=False)
    contexto_json: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
