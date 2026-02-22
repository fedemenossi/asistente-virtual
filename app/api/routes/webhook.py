from __future__ import annotations

import hmac
import logging
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.core.db import get_async_session
from app.core.tenancy import set_current_tenant_id
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.paciente_repository import PacienteRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.tenant_repository import TenantRepository
from app.models.tenant import Tenant
from app.schemas.webhook import TwilioWebhookPayload
from app.services.conversation_service import ConversationService
from app.services.tenant_service import TenantService

router = APIRouter()
logger = logging.getLogger(__name__)
RESTART_HINT = 'Escriba la palabra "salir" para reiniciar la conversacion y volver a comenzar.'


def _mask_phone(value: str | None) -> str:
    if not value:
        return "-"
    trimmed = value.strip()
    if len(trimmed) <= 6:
        return "***"
    return f"{trimmed[:4]}***{trimmed[-2:]}"


def _candidate_validation_urls(request: Request) -> list[str]:
    """
    Twilio signs the public URL; behind reverse proxies request.url may appear as http.
    Try common proxy/public variants to avoid false negatives.
    """
    current_url = str(request.url)
    candidates = [current_url]

    split = urlsplit(current_url)
    if split.scheme == "http":
        candidates.append(urlunsplit(("https", split.netloc, split.path, split.query, split.fragment)))

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        proto = (forwarded_proto or split.scheme or "https").split(",")[0].strip() or "https"
        candidates.append(urlunsplit((proto, forwarded_host, split.path, split.query, split.fragment)))
        if proto != "https":
            candidates.append(urlunsplit(("https", forwarded_host, split.path, split.query, split.fragment)))

    # preserve order but remove duplicates
    seen: set[str] = set()
    unique: list[str] = []
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


async def _process_whatsapp_webhook(
    request: Request,
    session: AsyncSession,
    resolved_tenant: Tenant | None = None,
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

    conversation_service = ConversationService(
        session=session,
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
        notification_repo=NotificationRepository(session),
    )

    tenant = resolved_tenant
    if tenant is None:
        tenant_repo = TenantRepository(session)
        tenant_service = TenantService(tenant_repo)
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

    tenant_whatsapp = tenant.whatsapp_settings or {}
    auth_token = (tenant_whatsapp.get("twilio_auth_token") or "").strip()
    if not auth_token:
        logger.warning(
            "whatsapp_webhook_tenant_missing_auth_token tenant_id=%s to=%s from=%s",
            tenant.id,
            _mask_phone(payload.to_number),
            _mask_phone(payload.from_number),
        )
        raise HTTPException(status_code=403, detail="Token Twilio no configurado para el tenant")
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
    validation_urls = _candidate_validation_urls(request)
    is_valid_signature = any(validator.validate(url, form, signature) for url in validation_urls)
    if not is_valid_signature:
        logger.warning(
            "whatsapp_webhook_signature_invalid tenant_id=%s has_signature=%s validation_urls=%s to=%s from=%s",
            tenant.id,
            bool(signature),
            validation_urls,
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


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    async with session.begin():
        return await _process_whatsapp_webhook(request, session)


@router.post("/webhook/whatsapp/{tenant_id}/{secret}")
async def whatsapp_webhook_by_tenant(
    tenant_id: int,
    secret: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    async with session.begin():
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None or tenant.deleted_at is not None or not tenant.activo:
            logger.warning("whatsapp_webhook_by_tenant_not_found tenant_id=%s", tenant_id)
            return _twilio_response("Numero no reconocido.")

        tenant_whatsapp = tenant.whatsapp_settings or {}
        configured_secret = (tenant_whatsapp.get("twilio_webhook_secret") or "").strip()
        if not configured_secret:
            logger.warning("whatsapp_webhook_by_tenant_missing_secret tenant_id=%s", tenant_id)
            raise HTTPException(status_code=403, detail="Webhook secret no configurado para el tenant")
        if not hmac.compare_digest(configured_secret, secret.strip()):
            logger.warning("whatsapp_webhook_by_tenant_invalid_secret tenant_id=%s", tenant_id)
            raise HTTPException(status_code=403, detail="Webhook secret invalido")

        return await _process_whatsapp_webhook(request, session, resolved_tenant=tenant)


def _twilio_response(message: str) -> Response:
    final_message = message.strip()
    if RESTART_HINT not in final_message:
        if final_message:
            final_message = f"{final_message}\n\n{RESTART_HINT}"
        else:
            final_message = RESTART_HINT
    logger.info(
        "whatsapp_webhook_twiml_reply len=%s preview=%s",
        len(final_message),
        final_message[:160].replace("\n", " | "),
    )
    response = MessagingResponse()
    response.message(final_message)
    return Response(content=str(response), media_type="application/xml")
