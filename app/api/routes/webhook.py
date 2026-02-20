from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.core.config import get_settings
from app.core.db import get_async_session
from app.core.tenancy import set_current_tenant_id
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.paciente_repository import PacienteRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.webhook import TwilioWebhookPayload
from app.services.conversation_service import ConversationService
from app.services.tenant_service import TenantService

router = APIRouter()
logger = logging.getLogger(__name__)


def _mask_phone(value: str | None) -> str:
    if not value:
        return "-"
    trimmed = value.strip()
    if len(trimmed) <= 6:
        return "***"
    return f"{trimmed[:4]}***{trimmed[-2:]}"


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    form = dict(await request.form())
    signature = request.headers.get("X-Twilio-Signature", "")
    logger.info(
        "whatsapp_webhook_received method=%s path=%s content_type=%s has_signature=%s form_keys=%s",
        request.method,
        request.url.path,
        request.headers.get("content-type", ""),
        bool(signature),
        ",".join(sorted(form.keys())),
    )
    try:
        payload = TwilioWebhookPayload.model_validate(form)
    except ValidationError:
        logger.exception("whatsapp_webhook_invalid_payload")
        raise HTTPException(status_code=400, detail="Payload invalido")

    tenant_repo = TenantRepository(session)
    conversation_service = ConversationService(
        session=session,
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
        notification_repo=NotificationRepository(session),
    )
    tenant_service = TenantService(tenant_repo)

    async with session.begin():
        try:
            tenant = await tenant_service.resolve_by_whatsapp(payload.to_number)
        except SQLAlchemyError:
            logger.exception(
                "whatsapp_webhook_db_error_resolving_tenant to=%s from=%s",
                _mask_phone(payload.to_number),
                _mask_phone(payload.from_number),
            )
            raise
        if tenant is None or not tenant.activo:
            logger.warning(
                "whatsapp_webhook_tenant_not_found_or_inactive to=%s from=%s",
                _mask_phone(payload.to_number),
                _mask_phone(payload.from_number),
            )
            return _twilio_response("Numero no reconocido.")

        settings = get_settings()
        tenant_whatsapp = tenant.whatsapp_settings or {}
        auth_token = tenant_whatsapp.get("twilio_auth_token") or settings.twilio_auth_token
        using_tenant_token = bool(tenant_whatsapp.get("twilio_auth_token"))
        logger.info(
            "whatsapp_webhook_tenant_resolved tenant_id=%s tenant_active=%s using_tenant_token=%s to=%s from=%s",
            tenant.id,
            tenant.activo,
            using_tenant_token,
            _mask_phone(payload.to_number),
            _mask_phone(payload.from_number),
        )
        validator = RequestValidator(auth_token)
        is_valid_signature = validator.validate(str(request.url), form, signature)
        if not is_valid_signature:
            logger.warning(
                "whatsapp_webhook_signature_invalid tenant_id=%s has_signature=%s url=%s to=%s from=%s",
                tenant.id,
                bool(signature),
                str(request.url),
                _mask_phone(payload.to_number),
                _mask_phone(payload.from_number),
            )
            raise HTTPException(status_code=403, detail="Firma Twilio invalida")

        set_current_tenant_id(tenant.id)
        try:
            logger.info(
                "whatsapp_webhook_processing tenant_id=%s from=%s body_len=%s",
                tenant.id,
                _mask_phone(payload.from_number),
                len((payload.body or "").strip()),
            )
            reply_text = await conversation_service.process_message(
                tenant=tenant,
                from_phone=payload.from_number,
                body=payload.body,
            )
            logger.info(
                "whatsapp_webhook_processed tenant_id=%s from=%s reply_len=%s",
                tenant.id,
                _mask_phone(payload.from_number),
                len((reply_text or "").strip()),
            )
        finally:
            set_current_tenant_id(None)

    return _twilio_response(reply_text)


def _twilio_response(message: str) -> Response:
    response = MessagingResponse()
    response.message(message)
    return Response(content=str(response), media_type="application/xml")
