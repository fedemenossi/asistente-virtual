from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class EstadoConversacion(Base):
    __tablename__ = "estados_conversacion"

    telefono: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    estado_actual: Mapped[str] = mapped_column(String(50), nullable=False)
    contexto_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str | None] = mapped_column(String(20), default="active")
    pending_reason: Mapped[str | None] = mapped_column(String(50))
    pending_message: Mapped[str | None] = mapped_column(String(500))
    conversation_category: Mapped[str | None] = mapped_column(String(64))
    conversation_subtype: Mapped[str | None] = mapped_column(String(64))
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    last_patient_message: Mapped[str | None] = mapped_column(String(1000))
    media_metadata: Mapped[dict | list | None] = mapped_column(JSON)
    pending_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
