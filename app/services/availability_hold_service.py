from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_ba
from app.models.availability_hold import AvailabilityHold, AvailabilityHoldStatus
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.paciente import Paciente
from app.models.payment import Payment, PaymentStatus
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, EstadoTurno, TipoTurno, Turno
from app.services.appointment_service import AppointmentService


class SlotAlreadyHeldError(RuntimeError):
    pass


class AvailabilityHoldExpiredError(RuntimeError):
    pass


class AvailabilityHoldService:
    HOLD_DURATION = timedelta(hours=1)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(
        self,
        *,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
        conversation_phone: str,
        external_slot_id: str,
        start_at: datetime,
        end_at: datetime,
        timezone_name: str,
        payment_provider: str = "simulator",
    ) -> tuple[AvailabilityHold, Turno, Payment]:
        existing = await self._find_slot_hold(tenant.id, consultorio.id, external_slot_id)
        current = now_ba()
        if existing is not None and self._is_active(existing, current):
            if existing.conversation_phone != conversation_phone:
                raise SlotAlreadyHeldError(external_slot_id)
            turno = await self._session.get(Turno, existing.turno_id)
            payment = await self._payment_for_turno(existing.turno_id)
            if turno is not None and payment is not None:
                return existing, turno, payment

        if existing is not None:
            old_turno = await self._session.get(Turno, existing.turno_id)
            if old_turno is not None and old_turno.status == AppointmentStatus.WAITING_PAYMENT:
                old_turno.status = AppointmentStatus.CANCELLED
                old_turno.estado = EstadoTurno.CANCELADO
                old_turno.external_status = "hold_expired"
                old_turno.cancelled_at = current
                old_turno.cancellation_reason = "hold_expired"

        tipo = (
            TipoTurno.VIRTUAL
            if consultorio.tipo == TipoConsultorio.VIRTUAL
            else TipoTurno.PRESENCIAL
        )
        turno = await AppointmentService(self._session).create_local_turno(
            tenant=tenant,
            consultorio=consultorio,
            paciente=paciente,
            tipo=tipo,
            start_at=start_at,
            end_at=end_at,
            timezone_name=timezone_name,
            external_id=external_slot_id,
            external_status="held_waiting_payment",
            status=AppointmentStatus.WAITING_PAYMENT,
            estado=EstadoTurno.WAITING_PAYMENT,
            notes="Disponibilidad retenida por una hora; pendiente de pago.",
        )
        amount, currency = self.first_consultation_fee(tenant)
        payment = Payment(
            tenant_id=tenant.id,
            patient_id=paciente.id,
            appointment_id=turno.id,
            provider=payment_provider,
            status=PaymentStatus.PENDING,
            amount=amount,
            currency=currency,
            description=(
                "Primera consulta particular (pago simulado)"
                if payment_provider == "simulator"
                else "Primera consulta particular"
            ),
        )
        self._session.add(payment)
        await self._session.flush()

        expires_at = current + self.HOLD_DURATION
        if existing is None:
            hold = AvailabilityHold(
                tenant_id=tenant.id,
                consultorio_id=consultorio.id,
                patient_id=paciente.id,
                turno_id=turno.id,
                conversation_phone=conversation_phone,
                external_slot_id=external_slot_id,
                start_at=start_at,
                end_at=end_at,
                timezone=timezone_name,
                expires_at=expires_at,
                status=AvailabilityHoldStatus.ACTIVE,
            )
            try:
                async with self._session.begin_nested():
                    self._session.add(hold)
                    await self._session.flush()
            except IntegrityError:
                # Otra conversacion gano la restriccion unica entre la consulta
                # inicial y el INSERT. Eliminamos los registros locales que esta
                # tentativa ya habia creado y devolvemos un conflicto de dominio.
                await self._session.delete(payment)
                await self._session.delete(turno)
                await self._session.flush()
                winner = await self._find_slot_hold(
                    tenant.id, consultorio.id, external_slot_id
                )
                if winner is not None and self._is_active(winner, now_ba()):
                    if winner.conversation_phone == conversation_phone:
                        winner_turno = await self._session.get(Turno, winner.turno_id)
                        winner_payment = await self._payment_for_turno(winner.turno_id)
                        if winner_turno is not None and winner_payment is not None:
                            return winner, winner_turno, winner_payment
                    raise SlotAlreadyHeldError(external_slot_id)
                raise
        else:
            hold = existing
            hold.patient_id = paciente.id
            hold.turno_id = turno.id
            hold.conversation_phone = conversation_phone
            hold.start_at = start_at
            hold.end_at = end_at
            hold.timezone = timezone_name
            hold.expires_at = expires_at
            hold.status = AvailabilityHoldStatus.ACTIVE
            await self._session.flush()
        return hold, turno, payment

    async def approve_simulated(
        self,
        *,
        hold: AvailabilityHold,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
    ) -> tuple[Turno, Payment, bool]:
        turno = await self._session.get(Turno, hold.turno_id)
        payment = await self._payment_for_turno(hold.turno_id)
        if turno is None or payment is None or payment.provider != "simulator":
            raise AvailabilityHoldExpiredError("hold_not_found")
        if turno.status == AppointmentStatus.CONFIRMED:
            return turno, payment, True
        if not self._is_active(hold, now_ba()):
            hold.status = AvailabilityHoldStatus.RELEASED
            turno.status = AppointmentStatus.CANCELLED
            turno.estado = EstadoTurno.CANCELADO
            turno.external_status = "hold_expired"
            turno.cancelled_at = now_ba()
            turno.cancellation_reason = "hold_expired"
            payment.status = PaymentStatus.CANCELLED
            await self._session.flush()
            raise AvailabilityHoldExpiredError("hold_expired")

        payment.status = PaymentStatus.APPROVED
        await AppointmentService(self._session).confirm_after_payment(
            None,
            tenant,
            consultorio,
            paciente,
            turno,
            commit=False,
        )
        if turno.status == AppointmentStatus.CONFIRMED:
            hold.status = AvailabilityHoldStatus.CONVERTED
            await self._session.flush()
            return turno, payment, False
        hold.status = AvailabilityHoldStatus.RELEASED
        await self._session.flush()
        return turno, payment, False

    async def expire_overdue(self) -> int:
        current = now_ba()
        result = await self._session.execute(
            select(AvailabilityHold)
            .where(
                AvailabilityHold.status == AvailabilityHoldStatus.ACTIVE,
                AvailabilityHold.expires_at <= current,
            )
            .with_for_update(skip_locked=True)
        )
        expired = 0
        for hold in result.scalars().all():
            turno = await self._session.get(Turno, hold.turno_id)
            hold.status = AvailabilityHoldStatus.RELEASED
            if turno is not None and turno.status == AppointmentStatus.WAITING_PAYMENT:
                turno.status = AppointmentStatus.CANCELLED
                turno.estado = EstadoTurno.CANCELADO
                turno.external_status = "hold_expired"
                turno.cancelled_at = current
                turno.cancellation_reason = "hold_expired"
            expired += 1
        await self._session.flush()
        return expired

    async def get(self, hold_id: int) -> AvailabilityHold | None:
        result = await self._session.execute(
            select(AvailabilityHold)
            .where(AvailabilityHold.id == hold_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _find_slot_hold(
        self, tenant_id: int, consultorio_id: int, external_slot_id: str
    ) -> AvailabilityHold | None:
        result = await self._session.execute(
            select(AvailabilityHold).where(
                AvailabilityHold.tenant_id == tenant_id,
                AvailabilityHold.consultorio_id == consultorio_id,
                AvailabilityHold.external_slot_id == external_slot_id,
            ).with_for_update()
        )
        return result.scalar_one_or_none()

    async def _payment_for_turno(self, turno_id: int) -> Payment | None:
        result = await self._session.execute(
            select(Payment).where(
                Payment.appointment_id == turno_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _is_active(hold: AvailabilityHold, current: datetime) -> bool:
        expires_at = hold.expires_at
        if expires_at.tzinfo is None and current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        return hold.status == AvailabilityHoldStatus.ACTIVE and expires_at > current

    @staticmethod
    def first_consultation_fee(tenant: Tenant) -> tuple[Decimal, str]:
        settings = tenant.payment_settings or {}
        raw_amount = settings.get("first_consultation_amount", settings.get("amount", "0"))
        try:
            amount = Decimal(str(raw_amount or "0"))
        except InvalidOperation:
            amount = Decimal("0")
        currency = str(
            settings.get("first_consultation_currency", settings.get("currency", "ARS")) or "ARS"
        ).strip().upper()
        return amount, currency
