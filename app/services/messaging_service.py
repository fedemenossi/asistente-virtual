from __future__ import annotations

import logging

from twilio.rest import Client

from app.core.config import get_settings

logger = logging.getLogger("messaging")


class MessagingService:
    def __init__(self) -> None:
        self._settings = get_settings()

    def send_whatsapp(self, to_number: str, message: str) -> None:
        if not self._settings.twilio_account_sid or not self._settings.twilio_auth_token:
            logger.warning("Twilio no configurado, omitido envio WhatsApp")
            return
        from_number = self._settings.twilio_whatsapp_number
        if not from_number:
            logger.warning("Twilio WhatsApp number no configurado")
            return
        to_formatted = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
        try:
            client = Client(self._settings.twilio_account_sid, self._settings.twilio_auth_token)
            client.messages.create(from_=from_number, to=to_formatted, body=message)
        except Exception:
            logger.exception("Error enviando WhatsApp")

    def send_email(self, to_email: str, subject: str, body: str) -> None:
        logger.info("Email pendiente de integrar: %s", to_email)
