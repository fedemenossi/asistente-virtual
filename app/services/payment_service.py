from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.config import get_settings
from app.integrations.mercadopago_service import MercadoPagoService, resolve_mp_credentials
from app.models.payment import Payment, PaymentStatus
from app.models.payment_event import PaymentEvent
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import EstadoTurno, Turno
from app.repositories.notification_repository import NotificationRepository
from app.repositories.payment_event_repository import PaymentEventRepository
from app.repositories.payment_repository import PaymentRepository


class PaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._settings = get_settings()

    async def create_payment_for_turno(
        self,
        request: Request,
        tenant: Tenant,
        paciente: Paciente,
        turno: Turno,
        amount: float,
        currency: str,
        description: str,
    ) -> Payment:
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
        await self._session.commit()
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
            repo = PaymentRepository(self._session)
            payment = await repo.get_by_id(payment_id)
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

        tenant = await self._session.get(Tenant, payment.tenant_id)
        payment_settings = tenant.payment_settings if tenant else {}
        access_token, webhook_secret = resolve_mp_credentials(payment_settings)
        signature = request.headers.get("x-signature", "")
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
            status_value = payment_info.get("status")

        new_status = MercadoPagoService.map_mp_status_to_internal(status_value)
        event_repo = PaymentEventRepository(self._session)
        if await event_repo.exists(payment.id, event_type, str(mp_payment_id) if mp_payment_id else None):
            if mp_payment_id and not payment.external_payment_id:
                payment.external_payment_id = str(mp_payment_id)
                await self._session.commit()
            return

        event = PaymentEvent(
            payment_id=payment.id,
            event_type=event_type,
            external_event_id=str(mp_payment_id) if mp_payment_id else None,
            payload_json=payload,
        )
        self._session.add(event)

        if payment.status != new_status:
            old_status = payment.status
            payment.status = new_status
            await audit_log(
                self._session,
                request,
                None,
                action="payment_status_change",
                entity="payment",
                entity_id=payment.id,
                tenant_id=payment.tenant_id,
                metadata={"from": old_status, "to": new_status},
            )

        notifier = NotificationRepository(self._session)
        await self._emit_payment_notifications(notifier, payment, new_status)

        if payment.appointment_id:
            turno = await self._session.get(Turno, payment.appointment_id)
            if turno:
                if new_status == PaymentStatus.APPROVED:
                    turno.estado = EstadoTurno.CONFIRMADO
                elif new_status in {PaymentStatus.REJECTED, PaymentStatus.CANCELLED}:
                    turno.estado = EstadoTurno.PAYMENT_FAILED
                else:
                    turno.estado = EstadoTurno.WAITING_PAYMENT
        await self._session.commit()

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
        if mp_payment_id and not payment.external_payment_id:
            payment.external_payment_id = str(mp_payment_id)
