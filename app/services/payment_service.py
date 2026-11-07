from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.config import get_settings
from app.integrations.mercadopago_service import MercadoPagoService, resolve_mp_credentials
from app.models.payment import Payment, PaymentStatus
from app.models.payment_event import PaymentEvent
from app.models.availability_hold import AvailabilityHold, AvailabilityHoldStatus
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.patient_credit import PatientCredit, PatientCreditStatus
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, EstadoTurno, Turno
from app.repositories.notification_repository import NotificationRepository
from app.repositories.payment_event_repository import PaymentEventRepository
from app.repositories.payment_repository import PaymentRepository
from app.services.appointment_service import AppointmentService
from app.services.messaging_service import MessagingService
from app.core.timezone import now_ba


class PaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._settings = get_settings()

    @staticmethod
    def is_configured(tenant: Tenant) -> bool:
        payment_settings = tenant.payment_settings or {}
        access_token, _ = resolve_mp_credentials(payment_settings)
        return bool(payment_settings.get("enabled") and access_token)

    async def create_payment_for_turno(
        self,
        request: Request | None,
        tenant: Tenant,
        paciente: Paciente,
        turno: Turno,
        amount: float,
        currency: str,
        description: str,
        *,
        payment: Payment | None = None,
        commit: bool = True,
    ) -> Payment:
        if payment is None:
            payment = Payment(
                tenant_id=tenant.id,
                patient_id=paciente.id,
                appointment_id=turno.id,
                provider="mercadopago",
                amount=amount,
                currency=currency,
                description=description,
                status=PaymentStatus.PENDING,
            )
            self._session.add(payment)
            await self._session.flush()
        elif (
            payment.tenant_id != tenant.id
            or payment.patient_id != paciente.id
            or payment.appointment_id != turno.id
            or payment.provider != "mercadopago"
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El pago no corresponde al turno, paciente y tenant indicados",
            )
        if payment.payment_url:
            return payment

        payment_settings = tenant.payment_settings or {}
        if not payment_settings.get("enabled"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Pagos no habilitados para este tenant",
            )
        access_token, _ = resolve_mp_credentials(payment_settings)
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mercado Pago no configurado",
            )

        mp_service = MercadoPagoService(access_token)
        notification_url = None
        if self._settings.public_base_url:
            notification_url = f"{self._settings.public_base_url}/webhook/payments/mercadopago?payment_id={payment.id}"

        preference = await mp_service.create_payment_preference(
            amount=amount,
            currency=currency,
            description=description,
            external_reference=str(payment.id),
            notification_url=notification_url,
        )
        payment.payment_url = preference.get("init_point")
        await audit_log(
            self._session,
            request,
            None,
            action="create_payment",
            entity="payment",
            entity_id=payment.id,
            tenant_id=tenant.id,
            metadata={"turno_id": turno.id},
        )
        notifier = NotificationRepository(self._session)
        await notifier.create(
            title="Pago pendiente",
            message=f"Se creo un pago para el turno #{turno.id}",
            notif_type="info",
            tenant_id=tenant.id,
        )
        turno.estado = EstadoTurno.WAITING_PAYMENT
        turno.status = AppointmentStatus.WAITING_PAYMENT
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return payment

    async def handle_mp_webhook(
        self,
        request: Request,
        payload: dict[str, Any],
        raw_body: bytes,
        payment_id: int | None,
    ) -> None:
        event_type = payload.get("type") or payload.get("action") or "unknown"
        mp_payment_id = None
        data = payload.get("data") or {}
        if isinstance(data, dict):
            mp_payment_id = data.get("id")

        payment = None
        if payment_id:
            payment = (
                await self._session.execute(
                    select(Payment).where(Payment.id == payment_id).with_for_update()
                )
            ).scalar_one_or_none()
        if not payment and mp_payment_id:
            payment = await PaymentRepository(self._session).get_by_external_id(
                "mercadopago", str(mp_payment_id)
            )

        if not payment and mp_payment_id and self._settings.mp_access_token:
            mp_service = MercadoPagoService(self._settings.mp_access_token)
            payment_info = await mp_service.get_payment(str(mp_payment_id))
            external_ref = payment_info.get("external_reference")
            if external_ref and external_ref.isdigit():
                payment = await PaymentRepository(self._session).get_by_id(int(external_ref))

        if not payment:
            return
        if not payment_id:
            payment = (
                await self._session.execute(
                    select(Payment).where(Payment.id == payment.id).with_for_update()
                )
            ).scalar_one()

        tenant = await self._session.get(Tenant, payment.tenant_id)
        payment_settings = tenant.payment_settings if tenant else {}
        access_token, webhook_secret = resolve_mp_credentials(payment_settings)
        signature = request.headers.get("x-signature", "")
        if webhook_secret and not signature:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if webhook_secret and signature:
            if not MercadoPagoService.verify_webhook_signature(raw_body, signature, webhook_secret):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        payment_info = None
        status_value = payload.get("status")
        if self._settings.app_env.lower() == "test" and isinstance(data, dict):
            status_value = data.get("status") or status_value
        elif mp_payment_id and access_token:
            mp_service = MercadoPagoService(access_token)
            payment_info = await mp_service.get_payment(str(mp_payment_id))
            external_reference = str(payment_info.get("external_reference") or "")
            if external_reference != str(payment.id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El pago del proveedor no corresponde al pago local",
                )
            status_value = payment_info.get("status")

        new_status = MercadoPagoService.map_mp_status_to_internal(status_value)
        event_repo = PaymentEventRepository(self._session)
        if mp_payment_id and not payment.external_payment_id:
            payment.external_payment_id = str(mp_payment_id)
        if await event_repo.exists(payment.id, event_type, str(mp_payment_id) if mp_payment_id else None):
            await self._session.commit()
            return

        event = PaymentEvent(
            payment_id=payment.id,
            event_type=event_type,
            external_event_id=str(mp_payment_id) if mp_payment_id else None,
            payload_json=payload,
        )
        self._session.add(event)

        effective_status = self._resolve_status_transition(payment.status, new_status)
        status_changed = payment.status != effective_status
        if status_changed:
            old_status = payment.status
            payment.status = effective_status
            await audit_log(
                self._session,
                request,
                None,
                action="payment_status_change",
                entity="payment",
                entity_id=payment.id,
                tenant_id=payment.tenant_id,
                metadata={"from": old_status, "to": effective_status},
            )

        notifier = NotificationRepository(self._session)
        if status_changed:
            await self._emit_payment_notifications(notifier, payment, effective_status)

        if payment.appointment_id:
            turno = await self._session.get(Turno, payment.appointment_id)
            if turno:
                if effective_status == PaymentStatus.APPROVED:
                    consultorio = await self._session.get(Consultorio, turno.consultorio_id)
                    paciente = await self._session.get(Paciente, turno.paciente_id)
                    hold = await self._hold_for_turno(turno.id)
                    if turno.status == AppointmentStatus.CONFIRMED:
                        pass
                    elif hold is not None and not self._hold_is_active(hold):
                        hold.status = AvailabilityHoldStatus.RELEASED
                        turno.estado = EstadoTurno.CANCELADO
                        turno.status = AppointmentStatus.CANCELLED
                        turno.external_status = "hold_expired"
                        turno.cancelled_at = now_ba()
                        turno.cancellation_reason = "hold_expired"
                        credit, created = await self._create_late_payment_credit(payment)
                        if created and tenant and paciente:
                            MessagingService().send_whatsapp(
                                paciente.telefono,
                                (
                                    f"Tu pago fue aprobado despues de vencer la retencion. "
                                    f"No se recupero el horario. Tenes un saldo a favor de "
                                    f"{credit.currency} {credit.amount} valido por 7 dias."
                                ),
                                tenant,
                            )
                    elif tenant and consultorio and paciente:
                        await AppointmentService(self._session).confirm_after_payment(
                            request,
                            tenant,
                            consultorio,
                            paciente,
                            turno,
                            commit=False,
                        )
                        if turno.status == AppointmentStatus.CONFIRMED:
                            if hold is not None:
                                hold.status = AvailabilityHoldStatus.CONVERTED
                            MessagingService().send_whatsapp(
                                paciente.telefono,
                                f"Pago aprobado. Turno confirmado para el {turno.start_at or turno.fecha_hora}.",
                                tenant,
                            )
                        elif hold is not None:
                            hold.status = AvailabilityHoldStatus.RELEASED
                            if tenant and paciente:
                                MessagingService().send_whatsapp(
                                    paciente.telefono,
                                    (
                                        "El pago fue aprobado, pero el turno no pudo ser "
                                        "confirmado porque el horario dejo de estar disponible. "
                                        "El consultorio revisara el caso."
                                    ),
                                    tenant,
                                )
                    else:
                        turno.estado = EstadoTurno.CONFIRMADO
                        turno.status = AppointmentStatus.CONFIRMED
                        turno.external_status = turno.external_status or "confirmed"
                elif effective_status in {PaymentStatus.REJECTED, PaymentStatus.CANCELLED}:
                    if turno.status == AppointmentStatus.CONFIRMED:
                        await self._session.commit()
                        return
                    turno.estado = EstadoTurno.PAYMENT_FAILED
                    turno.status = AppointmentStatus.CANCELLED
                    turno.external_status = "payment_failed"
                elif turno.status != AppointmentStatus.CONFIRMED:
                    turno.estado = EstadoTurno.WAITING_PAYMENT
                    turno.status = AppointmentStatus.WAITING_PAYMENT
                    turno.external_status = "waiting_payment"
        await self._session.commit()

    async def _hold_for_turno(self, turno_id: int) -> AvailabilityHold | None:
        return (
            await self._session.execute(
                select(AvailabilityHold)
                .where(AvailabilityHold.turno_id == turno_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _create_late_payment_credit(
        self, payment: Payment
    ) -> tuple[PatientCredit, bool]:
        existing = (
            await self._session.execute(
                select(PatientCredit)
                .where(PatientCredit.source_payment_id == payment.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        current = now_ba()
        credit = PatientCredit(
            tenant_id=payment.tenant_id,
            patient_id=payment.patient_id,
            source_payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            status=PatientCreditStatus.ACTIVE,
            expires_at=current + timedelta(days=7),
        )
        self._session.add(credit)
        await self._session.flush()
        return credit, True

    @staticmethod
    def _hold_is_active(hold: AvailabilityHold) -> bool:
        current = now_ba()
        expires_at = hold.expires_at
        if expires_at.tzinfo is None and current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        return (
            hold.status == AvailabilityHoldStatus.ACTIVE
            and expires_at > current
        )

    @staticmethod
    def _resolve_status_transition(
        current: PaymentStatus, incoming: PaymentStatus
    ) -> PaymentStatus:
        if current == PaymentStatus.REFUNDED:
            return current
        if incoming == PaymentStatus.REFUNDED:
            return incoming
        if current == PaymentStatus.APPROVED:
            return current
        if incoming == PaymentStatus.APPROVED:
            return incoming
        if current in {PaymentStatus.REJECTED, PaymentStatus.CANCELLED} and incoming == PaymentStatus.PENDING:
            return current
        return incoming

    async def _emit_payment_notifications(
        self,
        notifier: NotificationRepository,
        payment: Payment,
        new_status: PaymentStatus,
    ) -> None:
        title = "Pago actualizado"
        message = f"Pago #{payment.id} ahora esta {new_status.value}"
        notif_type = "info"
        if new_status == PaymentStatus.APPROVED:
            notif_type = "success"
        if new_status in {PaymentStatus.REJECTED, PaymentStatus.CANCELLED}:
            notif_type = "warning"
        await notifier.create(
            title=title,
            message=message,
            notif_type=notif_type,
            tenant_id=payment.tenant_id,
        )
