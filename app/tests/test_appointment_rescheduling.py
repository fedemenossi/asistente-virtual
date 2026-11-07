from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.timezone import now_ba
from app.models.patient_credit import PatientCredit, PatientCreditStatus
from app.models.payment import Payment, PaymentStatus
from app.models.turno import AppointmentStatus
from app.services.appointment_service import AppointmentService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_turno


def test_reschedule_secures_replacement_before_releasing_origin_and_is_idempotent(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Reprogramacion", "whatsapp:+824"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Consultorio Reprogramacion",
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-24"}},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491182400001"))
    origin_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=now_ba().replace(tzinfo=None) + timedelta(days=3),
            status=AppointmentStatus.CONFIRMED,
            external_calendar_provider="google",
            external_calendar_id="calendar-24",
            external_event_id="origin-event-24",
            external_status="reserved",
            eligibility_context={
                "coverage": {"type": "prepaga", "entity": "OSDE"},
                "first_consultation": False,
                "modality": "presential",
            },
        )
    )
    new_start = now_ba() + timedelta(days=5)
    new_end = new_start + timedelta(minutes=30)
    effects: list[tuple[str, str]] = []

    async def reserve_slot(self, tenant, consultorio, slot_id, patient, metadata):
        effects.append(("reserve", slot_id))
        return {
            "event_id": slot_id,
            "calendar_id": "calendar-24",
            "start_at": new_start.isoformat(),
            "end_at": new_end.isoformat(),
            "timezone": "America/Argentina/Buenos_Aires",
        }

    async def cancel_slot(self, tenant, consultorio, provider, event_id):
        effects.append(("cancel", event_id))

    monkeypatch.setattr("app.services.calendar_service.CalendarService.reserve_slot", reserve_slot)
    monkeypatch.setattr("app.services.calendar_service.CalendarService.cancel_slot", cancel_slot)

    async def run():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant
        from app.models.turno import Turno

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            paciente = await session.get(Paciente, paciente_id)
            origin = await session.get(Turno, origin_id)
            payment = Payment(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                appointment_id=origin_id,
                provider="mercadopago",
                external_payment_id="approved-reschedule-24",
                status=PaymentStatus.APPROVED,
                amount=25000,
                currency="ARS",
            )
            credit_source_payment = Payment(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                appointment_id=None,
                provider="mercadopago",
                external_payment_id="credit-source-reschedule-24",
                status=PaymentStatus.APPROVED,
                amount=25000,
                currency="ARS",
            )
            unused_credit_source_payment = Payment(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                appointment_id=None,
                provider="mercadopago",
                external_payment_id="unused-credit-source-reschedule-24",
                status=PaymentStatus.APPROVED,
                amount=25000,
                currency="ARS",
            )
            session.add_all([payment, credit_source_payment, unused_credit_source_payment])
            await session.flush()
            consumed_at = now_ba().replace(tzinfo=None) - timedelta(days=1)
            expires_at = now_ba().replace(tzinfo=None) + timedelta(days=3)
            credit = PatientCredit(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                source_payment_id=credit_source_payment.id,
                amount=25000,
                currency="ARS",
                status=PatientCreditStatus.CONSUMED,
                expires_at=expires_at,
                consumed_at=consumed_at,
                consumed_turno_id=origin_id,
            )
            unused_expires_at = now_ba().replace(tzinfo=None) + timedelta(days=2)
            unused_credit = PatientCredit(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                source_payment_id=unused_credit_source_payment.id,
                amount=25000,
                currency="ARS",
                status=PatientCreditStatus.ACTIVE,
                expires_at=unused_expires_at,
            )
            session.add_all([credit, unused_credit])
            await session.flush()
            service = AppointmentService(session)
            replacement, _ = await service.reschedule_turno(
                request=None,
                tenant=tenant,
                paciente=paciente,
                origin=origin,
                consultorio=consultorio,
                slot_id="replacement-event-24",
                start_at=new_start,
                end_at=new_end,
                timezone_name="America/Argentina/Buenos_Aires",
            )
            repeated, _ = await service.reschedule_turno(
                request=None,
                tenant=tenant,
                paciente=paciente,
                origin=origin,
                consultorio=consultorio,
                slot_id="replacement-event-24",
                start_at=new_start,
                end_at=new_end,
                timezone_name="America/Argentina/Buenos_Aires",
            )
            await session.refresh(origin)
            await session.refresh(payment)
            await session.refresh(credit)
            await session.refresh(unused_credit)
            payment_count = len((await session.execute(select(Payment))).scalars().all())
            credit_count = len((await session.execute(select(PatientCredit))).scalars().all())
            return (
                origin,
                replacement,
                repeated,
                payment,
                credit,
                unused_credit,
                consumed_at,
                expires_at,
                unused_expires_at,
                payment_count,
                credit_count,
            )

    (
        origin,
        replacement,
        repeated,
        payment,
        credit,
        unused_credit,
        consumed_at,
        expires_at,
        unused_expires_at,
        payment_count,
        credit_count,
    ) = asyncio.run(run())

    assert effects == [
        ("reserve", "replacement-event-24"),
        ("cancel", "origin-event-24"),
    ]
    assert origin.status == AppointmentStatus.CANCELLED
    assert origin.cancellation_reason == "rescheduled"
    assert replacement.status == AppointmentStatus.CONFIRMED
    assert replacement.rescheduled_from_id == origin.id
    assert replacement.eligibility_context == origin.eligibility_context
    assert repeated.id == replacement.id
    assert payment.status == PaymentStatus.APPROVED
    assert payment.appointment_id == replacement.id
    assert payment.reuse_status is None
    assert payment_count == 3
    assert credit_count == 2
    assert credit.status == PatientCreditStatus.CONSUMED
    assert credit.consumed_turno_id == replacement.id
    assert credit.consumed_at == consumed_at
    assert credit.expires_at == expires_at
    assert unused_credit.status == PatientCreditStatus.ACTIVE
    assert unused_credit.consumed_turno_id is None
    assert unused_credit.expires_at == unused_expires_at

    late_webhook = {
        "type": "payment.updated",
        "data": {"id": "approved-reschedule-24", "status": "approved"},
    }
    first_webhook = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment.id}", json=late_webhook
    )
    repeated_webhook = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment.id}", json=late_webhook
    )
    assert first_webhook.status_code == 200
    assert repeated_webhook.status_code == 200

    async def load_economic_state():
        async with db_session() as session:
            refreshed_payment = await session.get(Payment, payment.id)
            payments = list((await session.execute(select(Payment))).scalars().all())
            credits = list((await session.execute(select(PatientCredit))).scalars().all())
            return refreshed_payment, payments, credits

    refreshed_payment, payments, credits = asyncio.run(load_economic_state())
    assert refreshed_payment.appointment_id == replacement.id
    assert len(payments) == 3
    assert len(credits) == 2


def test_reschedule_keeps_origin_when_replacement_reservation_fails(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Reprogramacion Fallida", "whatsapp:+825"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Consultorio Reprogramacion Fallida",
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-25"}},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491182500001"))
    origin_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=now_ba().replace(tzinfo=None) + timedelta(days=3),
            status=AppointmentStatus.CONFIRMED,
            external_calendar_provider="google",
            external_event_id="origin-event-25",
            external_status="reserved",
            eligibility_context={"first_consultation": False, "modality": "presential"},
        )
    )
    cancellations: list[str] = []
    attempts = 0

    async def rejected_reservation(self, tenant, consultorio, slot_id, patient, metadata):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("slot unavailable")
        return {
            "event_id": slot_id,
            "calendar_id": "calendar-25",
            "start_at": (now_ba() + timedelta(days=5)).isoformat(),
            "end_at": (now_ba() + timedelta(days=5, minutes=30)).isoformat(),
            "timezone": "America/Argentina/Buenos_Aires",
        }

    async def cancel_slot(self, tenant, consultorio, provider, event_id):
        cancellations.append(event_id)

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.reserve_slot", rejected_reservation
    )
    monkeypatch.setattr("app.services.calendar_service.CalendarService.cancel_slot", cancel_slot)

    async def run():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant
        from app.models.turno import Turno

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            paciente = await session.get(Paciente, paciente_id)
            origin = await session.get(Turno, origin_id)
            approved_payment = Payment(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                appointment_id=origin_id,
                provider="mercadopago",
                status=PaymentStatus.APPROVED,
                amount=18000,
                currency="ARS",
            )
            credit_source = Payment(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                appointment_id=None,
                provider="mercadopago",
                status=PaymentStatus.APPROVED,
                amount=18000,
                currency="ARS",
            )
            session.add_all([approved_payment, credit_source])
            await session.flush()
            consumed_credit = PatientCredit(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                source_payment_id=credit_source.id,
                amount=18000,
                currency="ARS",
                status=PatientCreditStatus.CONSUMED,
                expires_at=now_ba().replace(tzinfo=None) + timedelta(days=2),
                consumed_at=now_ba().replace(tzinfo=None) - timedelta(hours=2),
                consumed_turno_id=origin_id,
            )
            session.add(consumed_credit)
            await session.flush()
            with pytest.raises(RuntimeError, match="slot unavailable"):
                await AppointmentService(session).reschedule_turno(
                    request=None,
                    tenant=tenant,
                    paciente=paciente,
                    origin=origin,
                    consultorio=consultorio,
                    slot_id="replacement-event-25",
                    start_at=now_ba() + timedelta(days=5),
                    end_at=now_ba() + timedelta(days=5, minutes=30),
                    timezone_name="America/Argentina/Buenos_Aires",
                )
            assert origin.status == AppointmentStatus.CONFIRMED
            assert origin.cancellation_reason is None
            assert cancellations == []
            assert approved_payment.appointment_id == origin_id
            assert consumed_credit.consumed_turno_id == origin_id
            await session.flush()
            replacement, _ = await AppointmentService(session).reschedule_turno(
                request=None,
                tenant=tenant,
                paciente=paciente,
                origin=origin,
                consultorio=consultorio,
                slot_id="replacement-event-25",
                start_at=now_ba() + timedelta(days=5),
                end_at=now_ba() + timedelta(days=5, minutes=30),
                timezone_name="America/Argentina/Buenos_Aires",
            )
            await session.refresh(approved_payment)
            await session.refresh(consumed_credit)
            return (
                origin.status,
                origin.cancellation_reason,
                replacement,
                approved_payment,
                consumed_credit,
            )

    (
        origin_status,
        cancellation_reason,
        replacement,
        approved_payment,
        consumed_credit,
    ) = asyncio.run(run())

    assert attempts == 2
    assert cancellations == ["origin-event-25"]
    assert origin_status == AppointmentStatus.CANCELLED
    assert cancellation_reason == "rescheduled"
    assert replacement.status == AppointmentStatus.CONFIRMED
    assert approved_payment.appointment_id == replacement.id
    assert consumed_credit.consumed_turno_id == replacement.id
