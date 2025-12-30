from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, payment_id: int) -> Payment | None:
        result = await self._session.execute(select(Payment).where(Payment.id == payment_id))
        return result.scalar_one_or_none()

    async def get_by_external_id(self, provider: str, external_payment_id: str) -> Payment | None:
        stmt = select(Payment).where(
            Payment.provider == provider,
            Payment.external_payment_id == external_payment_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
