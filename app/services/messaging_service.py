from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

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
        provider = str(getattr(self._settings, "email_provider", "") or "").strip().lower()
        resend_api_key = getattr(self._settings, "resend_api_key", None)
        use_resend = provider == "resend" or (not provider and bool(resend_api_key))
        smtp_host = self._settings.smtp_host
        smtp_port = int(self._settings.smtp_port or 587)
        smtp_username = self._settings.smtp_username
        smtp_password = self._settings.smtp_password
        smtp_use_tls = bool(self._settings.smtp_use_tls)
        if use_resend:
            smtp_host = "smtp.resend.com"
            smtp_port = 587
            smtp_username = "resend"
            smtp_password = resend_api_key
            smtp_use_tls = True
            if not smtp_password:
                raise RuntimeError("RESEND_API_KEY no configurado.")
        if not smtp_host:
            raise RuntimeError("SMTP no configurado. Falta SMTP_HOST.")
        from_header = _resolve_from_header(
            email_from=getattr(self._settings, "email_from", None),
            smtp_from_email=self._settings.smtp_from_email,
            smtp_from_name=self._settings.smtp_from_name,
            app_name=self._settings.app_name,
            smtp_username=smtp_username,
        )

        message = EmailMessage()
        message["From"] = from_header
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

        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
            if smtp_use_tls:
                smtp.starttls()
            if smtp_username:
                smtp.login(smtp_username, smtp_password or "")
            smtp.send_message(message)


def _resolve_from_header(
    *,
    email_from: str | None,
    smtp_from_email: str | None,
    smtp_from_name: str | None,
    app_name: str,
    smtp_username: str | None,
) -> str:
    if email_from:
        _, parsed_email = parseaddr(email_from)
        if not parsed_email:
            raise RuntimeError("EMAIL_FROM invalido.")
        return email_from
    from_email = smtp_from_email or smtp_username
    if not from_email:
        raise RuntimeError("EMAIL_FROM, SMTP_FROM_EMAIL o SMTP_USERNAME no configurado.")
    from_name = smtp_from_name or app_name
    return formataddr((from_name, from_email))
