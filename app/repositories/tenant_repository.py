from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant

logger = logging.getLogger(__name__)


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_whatsapp_number(self, whatsapp_number: str) -> Tenant | None:
        logger.info(
            "tenant_lookup_by_whatsapp start number=%s",
            whatsapp_number,
        )
        stmt = select(Tenant).where(
            Tenant.whatsapp_number == whatsapp_number,
            Tenant.deleted_at.is_(None),
        )
        try:
            result = await self._session.execute(stmt)
            tenant = result.scalar_one_or_none()
            logger.info(
                "tenant_lookup_by_whatsapp end found=%s tenant_id=%s",
                bool(tenant),
                getattr(tenant, "id", None),
            )
            return tenant
        except SQLAlchemyError:
            logger.exception(
                "tenant_lookup_by_whatsapp db_error number=%s",
                whatsapp_number,
            )
            raise
