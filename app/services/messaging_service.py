from __future__ import annotations

import logging

from twilio.rest import Client

from app.core.config import get_settings
from app.models.tenant import Tenant

logger = logging.getLogger("messaging")


class MessagingService:
    def __init__(self) -> None:
        self._settings = get_settings()

    def _resolve_twilio_config(self, tenant: Tenant | None = None) -> tuple[str | None, str | None, str | None]:
        tenant_settings = (tenant.whatsapp_settings or {}) if tenant else {}
        sid = tenant_settings.get("twilio_account_sid") or self._settings.twilio_account_sid
        token = tenant_settings.get("twilio_auth_token") or self._settings.twilio_auth_token
        from_number = tenant_settings.get("twilio_whatsapp_number") or self._settings.twilio_whatsapp_number
        return sid, token, from_number

    def send_whatsapp(self, to_number: str, message: str, tenant: Tenant | None = None) -> None:
        sid, token, from_number = self._resolve_twilio_config(tenant)
        if not sid or not token:
            logger.warning("Twilio no configurado, omitido envio WhatsApp")
            return
        if not from_number:
            logger.warning("Twilio WhatsApp number no configurado")
            return
        to_formatted = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
        try:
            client = Client(sid, token)
            client.messages.create(from_=from_number, to=to_formatted, body=message)
        except Exception:
            logger.exception("Error enviando WhatsApp")

    def send_email(self, to_email: str, subject: str, body: str) -> None:
        logger.info("Email pendiente de integrar: %s", to_email)
