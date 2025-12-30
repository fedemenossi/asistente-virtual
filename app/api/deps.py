from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator

from app.core.config import get_settings
from app.core.db import get_async_session
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.paciente_repository import PacienteRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.tenant_repository import TenantRepository
from app.services.conversation_service import ConversationService
from app.services.tenant_service import TenantService

security = HTTPBasic()


async def get_tenant_service(
    session: AsyncSession = Depends(get_async_session),
) -> TenantService:
    return TenantService(TenantRepository(session))


async def get_conversation_service(
    session: AsyncSession = Depends(get_async_session),
) -> ConversationService:
    return ConversationService(
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
        notification_repo=NotificationRepository(session),
    )


def admin_basic_auth(
    credentials: HTTPBasicCredentials = Depends(security),
) -> None:
    settings = get_settings()
    if (
        credentials.username != settings.admin_user
        or credentials.password != settings.admin_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales invalidas",
            headers={"WWW-Authenticate": "Basic"},
        )


async def verify_twilio_signature(request: Request) -> dict:
    settings = get_settings()
    validator = RequestValidator(settings.twilio_auth_token)
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    if not validator.validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="Firma Twilio invalida")
    return dict(form)
