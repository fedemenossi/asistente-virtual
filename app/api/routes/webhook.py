from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.twiml.messaging_response import MessagingResponse

from app.api.deps import verify_twilio_signature
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


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    form: dict = Depends(verify_twilio_signature),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    payload = TwilioWebhookPayload.model_validate(form)

    tenant_repo = TenantRepository(session)
    conversation_service = ConversationService(
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
        notification_repo=NotificationRepository(session),
    )
    tenant_service = TenantService(tenant_repo)

    async with session.begin():
        tenant = await tenant_service.resolve_by_whatsapp(payload.to_number)
        if tenant is None or not tenant.activo:
            logger.warning("tenant_not_found", extra={"to_number": payload.to_number})
            return _twilio_response("Numero no reconocido.")

        set_current_tenant_id(tenant.id)
        try:
            reply_text = await conversation_service.process_message(
                tenant=tenant,
                from_phone=payload.from_number,
                body=payload.body,
            )
        finally:
            set_current_tenant_id(None)

    return _twilio_response(reply_text)


def _twilio_response(message: str) -> Response:
    response = MessagingResponse()
    response.message(message)
    return Response(content=str(response), media_type="application/xml")
