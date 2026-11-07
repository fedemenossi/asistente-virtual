from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.integrations.interfaces import CalendarSlot
from app.models.availability_hold import AvailabilityHold, AvailabilityHoldStatus
from app.models.consultorio import TipoConsultorio
from app.models.payment import Payment, PaymentStatus
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, Turno
from app.models.user import UserRole
from app.services.conversation_service import ConversationService
from app.tests.conftest import (
    create_consultorio,
    create_paciente,
    create_tenant,
    create_user,
    login,
)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_payment_condition_is_exclusively_first_private_consultation():
    particular = SimpleNamespace(
        health_coverage_type="particular", obra_social="Particular"
    )
    prepaga = SimpleNamespace(health_coverage_type="prepaga", obra_social="OSDE")
    obra_social = SimpleNamespace(
        health_coverage_type="obra_social", obra_social="IOMA"
    )

    assert ConversationService._appointment_requires_payment(
        {"first_time": True}, particular
    )
    assert not ConversationService._appointment_requires_payment(
        {"first_time": False}, particular
    )
    assert not ConversationService._appointment_requires_payment(
        {"first_time": True}, prepaga
    )
    assert not ConversationService._appointment_requires_payment(
        {"first_time": True}, obra_social
    )


def test_tenant_configures_first_consultation_fee(client, db_session):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Consultorio Tarifa", "whatsapp:+5491100002001")
    )
    asyncio.run(
        create_user(
            db_session,
            "tarifa@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tarifa@example.com", "secret-123")

    page = client.get("/t/settings/payments")
    assert page.status_code == 200
    assert "Tarifa de primera consulta particular" in page.text

    response = client.post(
        "/t/settings/payments",
        data={
            "csrf_token": _csrf(page.text),
            "first_consultation_amount": "25000.50",
            "first_consultation_currency": "ARS",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    async def _load_settings() -> dict:
        async with db_session() as session:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            ).scalar_one()
            return tenant.payment_settings or {}

    settings = asyncio.run(_load_settings())
    assert settings["first_consultation_amount"] == "25000.50"
    assert settings["first_consultation_currency"] == "ARS"

    refreshed = client.get("/t/settings/payments")
    assert 'name="first_consultation_amount" value="25000.50"' in refreshed.text


def test_simulator_holds_first_private_consultation_until_explicit_approval(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Consultorio Retencion",
            "whatsapp:+5491100002002",
            ai_settings={
                "enabled": True,
                "tools_enabled": True,
                "availability_lookup_enabled": True,
                "max_offered_slots": 3,
                "require_confirmation_before_booking": True,
            },
            calendar_settings={"default_timezone": "America/Argentina/Buenos_Aires"},
            payment_settings={
                "first_consultation_amount": "30000",
                "first_consultation_currency": "ARS",
            },
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Retencion",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-hold"}},
        )
    )
    phone = "5491100002002"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            obra_social="Particular",
            health_coverage_type="particular",
        )
    )
    competing_phone = "5491100002003"
    competing_patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            competing_phone,
            obra_social="Particular",
            health_coverage_type="particular",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "admin-retencion@example.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-retencion@example.com", "secret-123")

    start = now_ba() + timedelta(days=2)
    end = start + timedelta(minutes=30)
    reservation_calls: list[str] = []

    async def controlled_slots(self, tenant, consultorio, window_start, window_end):
        return [
            CalendarSlot(
                slot_id="google-slot-held",
                start_at=start,
                end_at=end,
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id="calendar-hold",
            )
        ]

    async def controlled_reservation(self, tenant, consultorio, slot_id, patient, metadata):
        reservation_calls.append(slot_id)
        return {
            "event_id": slot_id,
            "calendar_id": "calendar-hold",
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "timezone": "America/Argentina/Buenos_Aires",
        }

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.list_available_slots", controlled_slots
    )
    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.reserve_slot", controlled_reservation
    )

    page = client.get("/admin/chat-simulator")
    csrf = _csrf(page.text)

    def send(
        message: str | None = None,
        action: str | None = None,
        *,
        selected_phone: str = phone,
        selected_patient_id: int = patient_id,
    ):
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf},
            json={
                "to_number": "whatsapp:+5491100002002",
                "from_number": f"whatsapp:+{selected_phone}",
                "patient_id": selected_patient_id,
                "message": message,
                "action": action,
            },
        )
        assert response.status_code == 200
        return response.json()

    send("hola")
    send("1")
    send("2")
    send("1")
    offered = send("1")
    assert "turnos virtuales disponibles" in offered["reply"]
    send("1")

    def send_competing(message: str):
        return send(
            message,
            selected_phone=competing_phone,
            selected_patient_id=competing_patient_id,
        )

    send_competing("hola")
    send_competing("1")
    send_competing("2")
    send_competing("1")
    competing_availability = send_competing("1")
    assert "turnos virtuales disponibles" in competing_availability["reply"]
    send_competing("1")

    confirmation = send("1")
    assert "ARS 30000" in confirmation["reply"]
    assert "retenido durante 1 hora" in confirmation["reply"]
    assert confirmation["actions"] == [
        {"id": "approve_simulated_payment", "label": "Aprobar pago simulado"}
    ]
    assert reservation_calls == []

    competing_confirmation = send_competing("1")
    assert "retenido durante 1 hora" not in competing_confirmation["reply"]
    assert competing_confirmation["actions"] == []

    async def _load_records():
        async with db_session() as session:
            turnos = list(
                (
                    await session.execute(select(Turno).where(Turno.tenant_id == tenant_id))
                ).scalars()
            )
            payments = list(
                (
                    await session.execute(select(Payment).where(Payment.tenant_id == tenant_id))
                ).scalars()
            )
            holds = list(
                (
                    await session.execute(
                        select(AvailabilityHold).where(
                            AvailabilityHold.tenant_id == tenant_id
                        )
                    )
                ).scalars()
            )
            return turnos, payments, holds

    turnos, payments, holds = asyncio.run(_load_records())
    assert len(turnos) == 1
    assert turnos[0].status == AppointmentStatus.WAITING_PAYMENT
    assert len(payments) == 1
    assert payments[0].provider == "simulator"
    assert payments[0].status == PaymentStatus.PENDING
    assert len(holds) == 1
    assert holds[0].status == AvailabilityHoldStatus.ACTIVE

    approved = send(action="approve_simulated_payment")
    assert "Turno confirmado" in approved["reply"]
    repeated = send(action="approve_simulated_payment")
    assert "Turno confirmado" in repeated["reply"]
    assert reservation_calls == ["google-slot-held"]

    turnos, payments, holds = asyncio.run(_load_records())
    assert turnos[0].status == AppointmentStatus.CONFIRMED
    assert payments[0].status == PaymentStatus.APPROVED
    assert holds[0].status == AvailabilityHoldStatus.CONVERTED
