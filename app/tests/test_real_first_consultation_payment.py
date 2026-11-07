from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.core.timezone import now_ba
from app.models.availability_hold import AvailabilityHold, AvailabilityHoldStatus
from app.models.consultorio import TipoConsultorio
from app.models.patient_contact_link import PatientContactLink, PatientContactLinkStatus
from app.models.payment import Payment, PaymentStatus
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, EstadoTurno, Turno
from app.models.whatsapp_contact import WhatsappContact
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.paciente_repository import PacienteRepository
from app.services.conversation_service import ConversationService, ConversationState
from app.tests.conftest import (
    create_consultorio,
    create_paciente,
    create_payment,
    create_tenant,
    create_turno,
)


def test_whatsapp_first_private_consultation_returns_secure_payment_link(
    db_session, monkeypatch
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Cobro Real",
            "whatsapp:+5491100021001",
            payment_settings={
                "enabled": True,
                "mp_access_token": "tenant-access-token",
                "first_consultation_amount": "42000",
                "first_consultation_currency": "ARS",
            },
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Consultorio Virtual",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-real"}},
        )
    )
    phone = "5491100021001"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            obra_social="Particular",
            health_coverage_type="particular",
        )
    )
    start = now_ba() + timedelta(days=3)
    end = start + timedelta(minutes=30)
    preference_requests: list[dict] = []

    async def fake_preference(
        self,
        amount,
        currency,
        description,
        external_reference,
        notification_url=None,
        success_url=None,
        failure_url=None,
    ):
        preference_requests.append(
            {
                "amount": amount,
                "currency": currency,
                "external_reference": external_reference,
            }
        )
        return {"init_point": "https://payments.example.test/checkout/secure-21"}

    monkeypatch.setattr(
        "app.services.payment_service.MercadoPagoService.create_payment_preference",
        fake_preference,
    )

    async def _run_confirmation():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            contact = WhatsappContact(tenant_id=tenant_id, phone_normalized=phone)
            session.add(contact)
            await session.flush()
            session.add(
                PatientContactLink(
                    tenant_id=tenant_id,
                    contact_id=contact.id,
                    patient_id=patient_id,
                    status=PatientContactLinkStatus.APPROVED.value,
                )
            )
            await ConversacionRepository(session).upsert_state(
                tenant_id,
                phone,
                ConversationState.ASK_AI_BOOKING_CONFIRMATION.value,
                {
                    "patient_id": patient_id,
                    "reason": "turno_virtual",
                    "for_whom": "self",
                    "first_time": True,
                    "appointment": {
                        "type": "virtual",
                        "awaiting_booking_confirmation": True,
                        "selected_slot": {
                            "option": 1,
                            "label": "Lunes 10/11 a las 10:00",
                            "start_at": start.isoformat(),
                            "end_at": end.isoformat(),
                            "timezone": "America/Argentina/Buenos_Aires",
                            "provider": "google_calendar",
                            "metadata": {
                                "consultorio_id": consultorio_id,
                                "external_event_id": "google-real-slot-21",
                            },
                        },
                    },
                },
            )
            service = ConversationService(
                session=session,
                paciente_repo=PacienteRepository(session),
                conversacion_repo=ConversacionRepository(session),
            )
            reply = await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            await session.commit()
            return reply

    reply = asyncio.run(_run_confirmation())

    assert "ARS 42000" in reply
    assert "https://payments.example.test/checkout/secure-21" in reply
    assert "Todavia no fue confirmado" in reply
    assert len(preference_requests) == 1

    async def _load_records():
        async with db_session() as session:
            turno = (
                await session.execute(select(Turno).where(Turno.tenant_id == tenant_id))
            ).scalar_one()
            payment = (
                await session.execute(select(Payment).where(Payment.tenant_id == tenant_id))
            ).scalar_one()
            hold = (
                await session.execute(
                    select(AvailabilityHold).where(AvailabilityHold.tenant_id == tenant_id)
                )
            ).scalar_one()
            return turno, payment, hold

    turno, payment, hold = asyncio.run(_load_records())
    assert turno.status == AppointmentStatus.WAITING_PAYMENT
    assert payment.status == PaymentStatus.PENDING
    assert payment.provider == "mercadopago"
    assert payment.appointment_id == turno.id
    assert preference_requests[0]["external_reference"] == str(payment.id)
    assert hold.status == AvailabilityHoldStatus.ACTIVE
    assert hold.turno_id == turno.id


def test_webhook_rejects_provider_payment_linked_to_another_local_payment(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Asociacion Segura",
            "whatsapp:+5491100021008",
            payment_settings={"mp_access_token": "tenant-access-token"},
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(db_session, tenant_id, "Agenda Asociacion Segura")
    )
    patient_id = asyncio.run(
        create_paciente(db_session, tenant_id, "5491100021008")
    )
    turno_id = asyncio.run(
        create_turno(db_session, patient_id, consultorio_id)
    )
    payment_id = asyncio.run(
        create_payment(db_session, tenant_id, patient_id, turno_id)
    )

    async def mismatched_payment(self, payment_id):
        return {
            "id": payment_id,
            "status": "approved",
            "external_reference": str(int(payment_id) + 999),
        }

    monkeypatch.setattr(
        "app.services.payment_service.get_settings",
        lambda: SimpleNamespace(app_env="production", mp_access_token=None),
    )
    monkeypatch.setattr(
        "app.services.payment_service.MercadoPagoService.get_payment",
        mismatched_payment,
    )

    response = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment_id}",
        json={"type": "payment", "data": {"id": "50021"}},
    )
    assert response.status_code == 400

    async def _load_payment():
        async with db_session() as session:
            return await session.get(Payment, payment_id)

    payment = asyncio.run(_load_payment())
    assert payment.status == PaymentStatus.PENDING
    assert payment.external_payment_id is None


def test_approved_webhook_with_active_hold_confirms_once_and_ignores_older_status(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant Webhook Real", "whatsapp:+5491100021002")
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Agenda Webhook",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-webhook"}},
        )
    )
    phone = "5491100021002"
    patient_id = asyncio.run(create_paciente(db_session, tenant_id, phone))
    start = now_ba() + timedelta(days=2)
    end = start + timedelta(minutes=30)
    turno_id = asyncio.run(
        create_turno(
            db_session,
            patient_id,
            consultorio_id,
            start_at=start,
            end_at=end,
            estado=EstadoTurno.WAITING_PAYMENT,
            status=AppointmentStatus.WAITING_PAYMENT,
            provider="google",
            external_id="slot-webhook-21",
            external_calendar_provider="google",
            external_event_id="slot-webhook-21",
            external_status="held_waiting_payment",
        )
    )
    payment_id = asyncio.run(
        create_payment(db_session, tenant_id, patient_id, turno_id)
    )

    async def _create_hold():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    AvailabilityHold(
                        tenant_id=tenant_id,
                        consultorio_id=consultorio_id,
                        patient_id=patient_id,
                        turno_id=turno_id,
                        conversation_phone=phone,
                        external_slot_id="slot-webhook-21",
                        start_at=start,
                        end_at=end,
                        timezone="America/Argentina/Buenos_Aires",
                        expires_at=now_ba() + timedelta(hours=1),
                        status=AvailabilityHoldStatus.ACTIVE,
                    )
                )

    asyncio.run(_create_hold())
    reservation_calls: list[str] = []
    whatsapp_messages: list[str] = []

    async def reserve(self, tenant, consultorio, slot_id, patient, metadata):
        reservation_calls.append(slot_id)
        return {
            "event_id": slot_id,
            "calendar_id": "calendar-webhook",
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "timezone": "America/Argentina/Buenos_Aires",
        }

    def send_whatsapp(self, to_number, message, tenant=None):
        whatsapp_messages.append(message)

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.reserve_slot", reserve
    )
    monkeypatch.setattr(
        "app.services.payment_service.MessagingService.send_whatsapp", send_whatsapp
    )

    approved = {"type": "payment", "data": {"id": "mp-real-21", "status": "approved"}}
    duplicate = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment_id}", json=approved
    )
    assert duplicate.status_code == 200
    duplicate = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment_id}", json=approved
    )
    assert duplicate.status_code == 200
    older = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment_id}",
        json={
            "type": "payment.updated",
            "data": {"id": "mp-real-21", "status": "pending"},
        },
    )
    assert older.status_code == 200

    assert reservation_calls == ["slot-webhook-21"]
    assert len(whatsapp_messages) == 1
    assert "Turno confirmado" in whatsapp_messages[0]

    async def _load_result():
        async with db_session() as session:
            payment = await session.get(Payment, payment_id)
            turno = await session.get(Turno, turno_id)
            hold = (
                await session.execute(
                    select(AvailabilityHold).where(
                        AvailabilityHold.turno_id == turno_id
                    )
                )
            ).scalar_one()
            return payment, turno, hold

    payment, turno, hold = asyncio.run(_load_result())
    assert payment.status == PaymentStatus.APPROVED
    assert turno.status == AppointmentStatus.CONFIRMED
    assert hold.status == AvailabilityHoldStatus.CONVERTED


def test_internal_expiration_job_releases_overdue_hold_once(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant Expiracion", "whatsapp:+5491100021003")
    )
    consultorio_id = asyncio.run(
        create_consultorio(db_session, tenant_id, "Agenda Expiracion")
    )
    patient_id = asyncio.run(
        create_paciente(db_session, tenant_id, "5491100021003")
    )
    start = now_ba() + timedelta(days=1)
    turno_id = asyncio.run(
        create_turno(
            db_session,
            patient_id,
            consultorio_id,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            estado=EstadoTurno.WAITING_PAYMENT,
            status=AppointmentStatus.WAITING_PAYMENT,
            external_event_id="expired-slot-21",
            external_status="held_waiting_payment",
        )
    )
    payment_id = asyncio.run(
        create_payment(db_session, tenant_id, patient_id, turno_id)
    )

    async def _seed_expired_hold():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    AvailabilityHold(
                        tenant_id=tenant_id,
                        consultorio_id=consultorio_id,
                        patient_id=patient_id,
                        turno_id=turno_id,
                        conversation_phone="5491100021003",
                        external_slot_id="expired-slot-21",
                        start_at=start,
                        end_at=start + timedelta(minutes=30),
                        timezone="America/Argentina/Buenos_Aires",
                        expires_at=now_ba() - timedelta(minutes=1),
                        status=AvailabilityHoldStatus.ACTIVE,
                    )
                )

    asyncio.run(_seed_expired_hold())
    monkeypatch.setattr(
        "app.api.routes.internal.get_settings",
        lambda: SimpleNamespace(internal_job_token="job-secret"),
    )

    first = client.post(
        "/internal/payment-holds/expire",
        headers={"X-Internal-Token": "job-secret"},
    )
    assert first.status_code == 200
    assert first.json() == {"expired": 1}
    second = client.post(
        "/internal/payment-holds/expire",
        headers={"X-Internal-Token": "job-secret"},
    )
    assert second.status_code == 200
    assert second.json() == {"expired": 0}

    async def _load_expired():
        async with db_session() as session:
            turno = await session.get(Turno, turno_id)
            payment = await session.get(Payment, payment_id)
            hold = (
                await session.execute(
                    select(AvailabilityHold).where(
                        AvailabilityHold.turno_id == turno_id
                    )
                )
            ).scalar_one()
            return turno, payment, hold

    turno, payment, hold = asyncio.run(_load_expired())
    assert hold.status == AvailabilityHoldStatus.RELEASED
    assert turno.status == AppointmentStatus.CANCELLED
    assert turno.cancellation_reason == "hold_expired"
    assert payment.status == PaymentStatus.PENDING


def test_late_approved_payment_creates_one_seven_day_patient_credit(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant Pago Tardio", "whatsapp:+5491100021004")
    )
    consultorio_id = asyncio.run(
        create_consultorio(db_session, tenant_id, "Agenda Pago Tardio")
    )
    phone = "5491100021004"
    patient_id = asyncio.run(create_paciente(db_session, tenant_id, phone))
    start = now_ba() + timedelta(days=1)
    turno_id = asyncio.run(
        create_turno(
            db_session,
            patient_id,
            consultorio_id,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            estado=EstadoTurno.CANCELADO,
            status=AppointmentStatus.CANCELLED,
            external_event_id="late-slot-21",
            external_status="hold_expired",
            cancellation_reason="hold_expired",
            cancelled_at=now_ba(),
        )
    )
    payment_id = asyncio.run(
        create_payment(db_session, tenant_id, patient_id, turno_id)
    )

    async def _seed_released_hold():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    AvailabilityHold(
                        tenant_id=tenant_id,
                        consultorio_id=consultorio_id,
                        patient_id=patient_id,
                        turno_id=turno_id,
                        conversation_phone=phone,
                        external_slot_id="late-slot-21",
                        start_at=start,
                        end_at=start + timedelta(minutes=30),
                        timezone="America/Argentina/Buenos_Aires",
                        expires_at=now_ba() - timedelta(minutes=5),
                        status=AvailabilityHoldStatus.RELEASED,
                    )
                )

    asyncio.run(_seed_released_hold())
    reservation_calls: list[str] = []
    messages: list[str] = []

    async def reserve(self, tenant, consultorio, slot_id, patient, metadata):
        reservation_calls.append(slot_id)
        raise AssertionError("Un pago tardio no debe intentar recuperar el horario")

    def send_whatsapp(self, to_number, message, tenant=None):
        messages.append(message)

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.reserve_slot", reserve
    )
    monkeypatch.setattr(
        "app.services.payment_service.MessagingService.send_whatsapp", send_whatsapp
    )

    payload = {"type": "payment", "data": {"id": "mp-late-21", "status": "approved"}}
    for _ in range(2):
        response = client.post(
            f"/webhook/payments/mercadopago?payment_id={payment_id}", json=payload
        )
        assert response.status_code == 200

    assert reservation_calls == []
    assert len(messages) == 1
    assert "saldo a favor" in messages[0].lower()
    assert "Turno confirmado" not in messages[0]

    async def _load_credit():
        from app.models.patient_credit import PatientCredit

        async with db_session() as session:
            credits = list(
                (
                    await session.execute(
                        select(PatientCredit).where(
                            PatientCredit.tenant_id == tenant_id,
                            PatientCredit.patient_id == patient_id,
                        )
                    )
                ).scalars()
            )
            payment = await session.get(Payment, payment_id)
            turno = await session.get(Turno, turno_id)
            return credits, payment, turno

    credits, payment, turno = asyncio.run(_load_credit())
    assert len(credits) == 1
    credit = credits[0]
    assert credit.source_payment_id == payment_id
    assert credit.status.value == "active"
    remaining = credit.expires_at - credit.created_at
    assert timedelta(days=6, hours=23) <= remaining <= timedelta(days=7, minutes=1)
    assert payment.status == PaymentStatus.APPROVED
    assert turno.status == AppointmentStatus.CANCELLED


def test_patient_credit_is_non_transferable_single_use_and_expires(db_session):
    from app.models.patient_credit import PatientCredit, PatientCreditStatus
    from app.services.patient_credit_service import PatientCreditService

    tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant Credito", "whatsapp:+5491100021005")
    )
    consultorio_id = asyncio.run(
        create_consultorio(db_session, tenant_id, "Agenda Credito")
    )
    patient_id = asyncio.run(create_paciente(db_session, tenant_id, "5491100021005"))
    other_patient_id = asyncio.run(
        create_paciente(db_session, tenant_id, "5491100021006")
    )
    source_turno_id = asyncio.run(
        create_turno(db_session, patient_id, consultorio_id)
    )
    source_payment_id = asyncio.run(
        create_payment(db_session, tenant_id, patient_id, source_turno_id)
    )
    expired_source_turno_id = asyncio.run(
        create_turno(db_session, patient_id, consultorio_id)
    )
    expired_source_payment_id = asyncio.run(
        create_payment(
            db_session, tenant_id, patient_id, expired_source_turno_id
        )
    )
    target_turno_id = asyncio.run(
        create_turno(db_session, patient_id, consultorio_id)
    )
    second_turno_id = asyncio.run(
        create_turno(db_session, patient_id, consultorio_id)
    )
    other_turno_id = asyncio.run(
        create_turno(db_session, other_patient_id, consultorio_id)
    )

    async def _exercise_credit():
        async with db_session() as session:
            credit = PatientCredit(
                tenant_id=tenant_id,
                patient_id=patient_id,
                source_payment_id=source_payment_id,
                amount=100,
                currency="ARS",
                status=PatientCreditStatus.ACTIVE,
                expires_at=now_ba() + timedelta(days=7),
            )
            session.add(credit)
            expired_credit = PatientCredit(
                tenant_id=tenant_id,
                patient_id=patient_id,
                source_payment_id=expired_source_payment_id,
                amount=100,
                currency="ARS",
                status=PatientCreditStatus.ACTIVE,
                expires_at=now_ba() - timedelta(seconds=1),
            )
            session.add(expired_credit)
            await session.flush()
            service = PatientCreditService(session)
            target = await session.get(Turno, target_turno_id)
            second = await session.get(Turno, second_turno_id)
            other = await session.get(Turno, other_turno_id)
            consumed = await service.consume_available(
                tenant_id=tenant_id,
                patient_id=patient_id,
                turno=target,
                amount=100,
                currency="ARS",
            )
            repeated = await service.consume_available(
                tenant_id=tenant_id,
                patient_id=patient_id,
                turno=target,
                amount=100,
                currency="ARS",
            )
            second_attempt = await service.consume_available(
                tenant_id=tenant_id,
                patient_id=patient_id,
                turno=second,
                amount=100,
                currency="ARS",
            )
            transfer_attempt = await service.consume_available(
                tenant_id=tenant_id,
                patient_id=other_patient_id,
                turno=other,
                amount=100,
                currency="ARS",
            )
            await session.commit()
            return (
                consumed,
                repeated,
                second_attempt,
                transfer_attempt,
                expired_credit,
            )

    consumed, repeated, second_attempt, transfer_attempt, expired_credit = (
        asyncio.run(_exercise_credit())
    )
    assert consumed is not None
    assert repeated is not None and repeated.id == consumed.id
    assert second_attempt is None
    assert transfer_attempt is None
    assert consumed.status == PatientCreditStatus.CONSUMED
    assert consumed.consumed_turno_id == target_turno_id
    assert expired_credit.status == PatientCreditStatus.EXPIRED


def test_paid_reservation_failure_is_traced_without_false_confirmation(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant Falla Reserva", "whatsapp:+5491100021007")
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Agenda Falla Reserva",
            proveedor_turnos="google",
        )
    )
    phone = "5491100021007"
    patient_id = asyncio.run(create_paciente(db_session, tenant_id, phone))
    start = now_ba() + timedelta(days=2)
    turno_id = asyncio.run(
        create_turno(
            db_session,
            patient_id,
            consultorio_id,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            estado=EstadoTurno.WAITING_PAYMENT,
            status=AppointmentStatus.WAITING_PAYMENT,
            provider="google",
            external_calendar_provider="google",
            external_event_id="conflict-slot-21",
            external_status="held_waiting_payment",
        )
    )
    payment_id = asyncio.run(
        create_payment(db_session, tenant_id, patient_id, turno_id)
    )

    async def _seed_hold():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    AvailabilityHold(
                        tenant_id=tenant_id,
                        consultorio_id=consultorio_id,
                        patient_id=patient_id,
                        turno_id=turno_id,
                        conversation_phone=phone,
                        external_slot_id="conflict-slot-21",
                        start_at=start,
                        end_at=start + timedelta(minutes=30),
                        timezone="America/Argentina/Buenos_Aires",
                        expires_at=now_ba() + timedelta(minutes=30),
                        status=AvailabilityHoldStatus.ACTIVE,
                    )
                )

    asyncio.run(_seed_hold())
    messages: list[str] = []

    async def reject_reservation(self, tenant, consultorio, slot_id, patient, metadata):
        raise RuntimeError("slot ocupado")

    def send_whatsapp(self, to_number, message, tenant=None):
        messages.append(message)

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.reserve_slot",
        reject_reservation,
    )
    monkeypatch.setattr(
        "app.services.payment_service.MessagingService.send_whatsapp", send_whatsapp
    )

    response = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment_id}",
        json={"type": "payment", "data": {"id": "mp-conflict-21", "status": "approved"}},
    )
    assert response.status_code == 200
    assert len(messages) == 1
    assert "no pudo ser confirmado" in messages[0].lower()
    assert "Turno confirmado" not in messages[0]

    async def _load_failure():
        async with db_session() as session:
            turno = await session.get(Turno, turno_id)
            payment = await session.get(Payment, payment_id)
            hold = (
                await session.execute(
                    select(AvailabilityHold).where(
                        AvailabilityHold.turno_id == turno_id
                    )
                )
            ).scalar_one()
            return turno, payment, hold

    turno, payment, hold = asyncio.run(_load_failure())
    assert payment.status == PaymentStatus.APPROVED
    assert turno.status == AppointmentStatus.CANCELLED
    assert turno.external_status == "sync_failed"
    assert turno.cancellation_reason == "slot_unavailable"
    assert hold.status == AvailabilityHoldStatus.RELEASED
