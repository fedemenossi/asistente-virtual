from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.arca.wsaa_client import AccessTicket
from app.models.arca_access_ticket import ArcaAccessTicket
from app.services.billing_arca_settings_service import decrypt_secret, encrypt_secret


class ArcaTicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_ticket(
        self,
        tenant_id: int,
        represented_cuit: str,
        environment: str,
        service: str,
    ) -> AccessTicket | None:
        row = await self._session.scalar(
            select(ArcaAccessTicket).where(
                ArcaAccessTicket.tenant_id == tenant_id,
                ArcaAccessTicket.represented_cuit == represented_cuit,
                ArcaAccessTicket.environment == environment,
                ArcaAccessTicket.service == service,
            )
        )
        if row is None:
            return None
        token = decrypt_secret(row.token_encrypted)
        sign = decrypt_secret(row.sign_encrypted)
        if not token or not sign:
            return None
        return AccessTicket(
            token=token,
            sign=sign,
            expiration_time=row.expiration_time,
        )

    async def save_ticket(
        self,
        tenant_id: int,
        represented_cuit: str,
        environment: str,
        service: str,
        ticket: AccessTicket,
    ) -> None:
        row = await self._session.scalar(
            select(ArcaAccessTicket).where(
                ArcaAccessTicket.tenant_id == tenant_id,
                ArcaAccessTicket.represented_cuit == represented_cuit,
                ArcaAccessTicket.environment == environment,
                ArcaAccessTicket.service == service,
            )
        )
        if row is None:
            row = ArcaAccessTicket(
                tenant_id=tenant_id,
                represented_cuit=represented_cuit,
                environment=environment,
                service=service,
                token_encrypted=encrypt_secret(ticket.token),
                sign_encrypted=encrypt_secret(ticket.sign),
                expiration_time=ticket.expiration_time.replace(tzinfo=None)
                if ticket.expiration_time.tzinfo
                else ticket.expiration_time,
            )
            self._session.add(row)
            return
        row.token_encrypted = encrypt_secret(ticket.token)
        row.sign_encrypted = encrypt_secret(ticket.sign)
        row.expiration_time = (
            ticket.expiration_time.replace(tzinfo=None)
            if ticket.expiration_time.tzinfo
            else ticket.expiration_time
        )
