from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class ConversationHistory(Base):
    __tablename__ = "conversaciones_historial"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True, nullable=False)
    telefono: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("pacientes.id"), index=True)
    estado_actual: Mapped[str | None] = mapped_column(String(50))
    contexto_json: Mapped[dict | None] = mapped_column(JSON)
    previous_status: Mapped[str | None] = mapped_column(String(20))
    pending_reason: Mapped[str | None] = mapped_column(String(50))
    pending_message: Mapped[str | None] = mapped_column(String(500))
    conversation_category: Mapped[str | None] = mapped_column(String(64))
    conversation_subtype: Mapped[str | None] = mapped_column(String(64))
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    last_patient_message: Mapped[str | None] = mapped_column(String(1000))
    media_metadata: Mapped[dict | list | None] = mapped_column(JSON)
    pending_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    close_reason: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
