from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class WhatsappInboundMessage(Base, TimestampMixin):
    __tablename__ = "whatsapp_inbound_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "message_sid",
            name="uq_whatsapp_inbound_message_tenant_sid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    message_sid: Mapped[str] = mapped_column(String(64), nullable=False)
    from_phone: Mapped[str] = mapped_column(String(40), nullable=False)
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
