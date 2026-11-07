from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.core.timezone import now_ba
from app.models.payment import Payment, PaymentStatus
from app.models.turno import AppointmentStatus
from app.services.appointment_service import AppointmentService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_turno


def test_reschedule_secures_replacement_before_releasing_origin_and_is_idempotent(
    db_session, monkeypatch
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
            session.add(payment)
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
            return origin, replacement, repeated, payment

    origin, replacement, repeated, payment = asyncio.run(run())

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
            return origin.status, origin.cancellation_reason, replacement

    origin_status, cancellation_reason, replacement = asyncio.run(run())

    assert attempts == 2
    assert cancellations == ["origin-event-25"]
    assert origin_status == AppointmentStatus.CANCELLED
    assert cancellation_reason == "rescheduled"
    assert replacement.status == AppointmentStatus.CONFIRMED
