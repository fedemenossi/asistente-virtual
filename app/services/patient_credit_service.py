from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_ba
from app.models.patient_credit import PatientCredit, PatientCreditStatus
from app.models.turno import Turno


class PatientCreditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def consume_available(
        self,
        *,
        tenant_id: int,
        patient_id: int,
        turno: Turno | None,
        amount: float | Decimal,
        currency: str,
    ) -> PatientCredit | None:
        if (
            turno is None
            or turno.tenant_id != tenant_id
            or turno.paciente_id != patient_id
        ):
            return None

        repeated = (
            await self._session.execute(
                select(PatientCredit)
                .where(
                    PatientCredit.tenant_id == tenant_id,
                    PatientCredit.patient_id == patient_id,
                    PatientCredit.status == PatientCreditStatus.CONSUMED,
                    PatientCredit.consumed_turno_id == turno.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if repeated is not None:
            return repeated

        current = now_ba()
        expired_result = await self._session.execute(
            select(PatientCredit)
            .where(
                PatientCredit.tenant_id == tenant_id,
                PatientCredit.patient_id == patient_id,
                PatientCredit.status == PatientCreditStatus.ACTIVE,
                PatientCredit.expires_at <= current,
            )
            .with_for_update()
        )
        for expired in expired_result.scalars().all():
            expired.status = PatientCreditStatus.EXPIRED

        normalized_currency = str(currency or "").strip().upper()
        required_amount = Decimal(str(amount))
        available = (
            await self._session.execute(
                select(PatientCredit)
                .where(
                    PatientCredit.tenant_id == tenant_id,
                    PatientCredit.patient_id == patient_id,
                    PatientCredit.status == PatientCreditStatus.ACTIVE,
                    PatientCredit.expires_at > current,
                    PatientCredit.currency == normalized_currency,
                    PatientCredit.amount >= required_amount,
                )
                .order_by(PatientCredit.expires_at.asc(), PatientCredit.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if available is None:
            await self._session.flush()
            return None
        available.status = PatientCreditStatus.CONSUMED
        available.consumed_at = current
        available.consumed_turno_id = turno.id
        await self._session.flush()
        return available
