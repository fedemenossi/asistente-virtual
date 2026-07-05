from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

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

    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        *,
        html_body: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> None:
        if not self._settings.smtp_host:
            raise RuntimeError("SMTP no configurado. Falta SMTP_HOST.")
        from_email = self._settings.smtp_from_email or self._settings.smtp_username
        if not from_email:
            raise RuntimeError("SMTP_FROM_EMAIL o SMTP_USERNAME no configurado.")

        message = EmailMessage()
        from_name = self._settings.smtp_from_name or self._settings.app_name
        message["From"] = formataddr((from_name, from_email))
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
        for filename, content, mime_type in attachments or []:
            maintype, _, subtype = mime_type.partition("/")
            message.add_attachment(
                content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=filename,
            )

        with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=20) as smtp:
            if self._settings.smtp_use_tls:
                smtp.starttls()
            if self._settings.smtp_username:
                smtp.login(self._settings.smtp_username, self._settings.smtp_password or "")
            smtp.send_message(message)
