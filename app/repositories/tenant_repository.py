from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_whatsapp_number(self, whatsapp_number: str) -> Tenant | None:
        stmt = select(Tenant).where(
            Tenant.whatsapp_number == whatsapp_number,
            Tenant.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
