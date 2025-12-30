from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment_event import PaymentEvent


class PaymentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(
        self, payment_id: int, event_type: str, external_event_id: str | None
    ) -> bool:
        stmt = select(PaymentEvent).where(
            PaymentEvent.payment_id == payment_id,
            PaymentEvent.event_type == event_type,
            PaymentEvent.external_event_id == external_event_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None
