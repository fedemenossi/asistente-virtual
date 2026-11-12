from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.integrations.consultorio_movil import SlotSelection
from app.integrations.drapp_provider import DrAppProvider, _decode_selection, _encode_selection
from app.models.consultorio import TipoConsultorio
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, Turno
from app.models.user import UserRole
from app.repositories.conversacion_repository import ConversacionRepository
from app.services.conversation_service import ConversationState
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


def test_drapp_slot_reference_fits_turno_contract_and_preserves_normalized_fields():
    start = datetime(2026, 11, 10, 9, 15, tzinfo=timezone(timedelta(hours=-3)))
    selection = SlotSelection(
        number=4,
        start_at=start,
        end_at=start + timedelta(minutes=45),
        duration_minutes=45,
        timezone="America/Argentina/Buenos_Aires",
        label="Martes 10 de noviembre a las 09:15 - Consultorio DrApp Central",
    )

    reference = _encode_selection(selection)
    restored = _decode_selection(reference)

    assert len(reference) <= 200
    assert restored.number == 4
    assert restored.start_at == selection.start_at
    assert restored.end_at == selection.end_at
    assert restored.duration_minutes == 45
    assert restored.timezone == "America/Argentina/Buenos_Aires"


def test_operator_configures_drapp_as_consultorio_agenda_source(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant DrApp Config", "whatsapp:+825"))
    asyncio.run(
        create_user(
            db_session,
            "drapp-config@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "drapp-config@test.com", "secret-123")
    form = client.get("/t/consultorios/new")

    assert form.status_code == 200
    assert '<option value="drapp"' in form.text
    assert "DrApp" in form.text

    created = client.post(
        "/t/consultorios/new",
        data={
            "csrf_token": _csrf(form.text),
            "nombre": "DrApp Centro",
            "tipo": "presencial",
            "proveedor_turnos": "drapp",
            "cab_user": "operator@example.com",
            "cab_password": "drapp-secret",
            "cab_staff_id": "staff-825",
            "cab_days": "14",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    listing = client.get(created.headers["location"])
    edit_match = re.search(r'href="(/t/consultorios/\d+/edit)"', listing.text)
    assert edit_match is not None
    edit = client.get(edit_match.group(1))

    assert edit.status_code == 200
    assert '<option value="drapp" selected>' in edit.text
    assert 'name="cab_user" value="operator@example.com"' in edit.text
    assert 'name="cab_staff_id" value="staff-825"' in edit.text
    assert 'name="cab_days" value="14"' in edit.text


def test_operator_tests_drapp_connection_without_exposing_credentials(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant DrApp Test", "whatsapp:+827")
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "DrApp Test",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="drapp",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "drapp-test@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    captured = {}
    start = datetime(2026, 11, 10, 9, 15, tzinfo=timezone(timedelta(hours=-3)))

    def controlled_availability(tenant, consultorio, limit):
        captured["config"] = consultorio.configuracion_externa["drapp"].copy()
        captured["limit"] = limit
        return [
            SlotSelection(
                number=4,
                start_at=start,
                end_at=start + timedelta(minutes=45),
                duration_minutes=45,
                timezone="America/Argentina/Buenos_Aires",
                label="Martes 10/11 09:15",
            )
        ]

    monkeypatch.setattr(
        "app.web.tenant.views.list_next_presential_slots",
        controlled_availability,
    )
    login(client, "drapp-test@test.com", "secret-123")
    page = client.get(f"/t/consultorios/{consultorio_id}/edit")

    response = client.post(
        f"/t/consultorios/{consultorio_id}/test-provider",
        data={
            "csrf_token": _csrf(page.text),
            "proveedor_turnos": "drapp",
            "cab_user": "test-user",
            "cab_password": "test-password-private",
            "cab_staff_id": "staff-827",
        },
    )

    assert response.status_code == 200
    assert response.json()["slots"][0]["duration_minutes"] == 45
    assert captured["config"]["staff_id"] == "staff-827"
    assert captured["limit"] == 30
    assert "test-password-private" not in response.text


def test_simulator_reserves_drapp_slot_and_exposes_confirmed_trace_to_operator(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant DrApp Booking",
            "whatsapp:+826",
            ai_settings={
                "enabled": True,
                "tools_enabled": True,
                "availability_lookup_enabled": True,
                "max_offered_slots": 5,
                "require_confirmation_before_booking": True,
            },
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "DrApp Presencial",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="drapp",
            configuracion_externa={
                "drapp": {
                    "user": "controlled-user",
                    "password": "controlled-secret",
                    "staff_id": "staff-826",
                    "days": 21,
                },
                "eligibility": {
                    "accepts_first_consultation": True,
                    "coverage_types": ["prepaga"],
                    "coverage_entities": ["OSDE"],
                    "weekly_ranges": [],
                },
            },
        )
    )
    phone = "5491182600001"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            nombre="Dora",
            apellido="Paciente",
            dni="40555666",
            health_coverage_type="prepaga",
            obra_social="OSDE",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "drapp-simulator@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "drapp-operator@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    start = (now_ba() + timedelta(days=2)).replace(hour=10, minute=30, second=0, microsecond=0)
    controlled_slot = SlotSelection(
        number=17,
        start_at=start,
        end_at=start + timedelta(minutes=40),
        duration_minutes=40,
        timezone="America/Argentina/Buenos_Aires",
        label=f"{start.strftime('%d/%m')} 10:30",
    )
    reservations = []

    def controlled_availability(tenant, consultorio, limit):
        return [controlled_slot]

    def controlled_reservation(tenant, consultorio, selection, patient):
        reservations.append((tenant.id, consultorio.id, selection.number, patient.id))
        return {
            "cabildo_id": "drapp-booking-826",
            "start_at": selection.start_at,
            "end_at": selection.end_at,
            "timezone": selection.timezone,
        }

    monkeypatch.setattr(
        "app.integrations.consultorio_movil.list_next_presential_slots",
        controlled_availability,
    )
    monkeypatch.setattr(
        "app.integrations.consultorio_movil.reserve_presential_slot",
        controlled_reservation,
    )
    login(client, "drapp-simulator@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+826",
                "from_number": f"whatsapp:+{phone}",
                "patient_id": patient_id,
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("1")
    send("1")
    send("1")
    coverage_prompt = send("2")
    assert "1) OSDE (prepaga)" in coverage_prompt
    offer = send("1")
    assert "10:30" in offer
    assert "controlled-secret" not in offer

    assert "Confirmas que queres reservarlo" in send("1")
    confirmation = send("1")

    assert "Turno confirmado" in confirmation
    assert reservations == [(tenant_id, consultorio_id, 17, patient_id)]

    login(client, "drapp-operator@test.com", "secret-123")
    appointment_date = start.date().isoformat()
    appointments = client.get(
        f"/t/appointments?date_from={appointment_date}&date_to={appointment_date}"
    )
    detail_match = re.search(r'href="(/t/appointments/\d+)"', appointments.text)
    assert detail_match is not None
    detail = client.get(detail_match.group(1))

    assert detail.status_code == 200
    assert "DrApp" in detail.text
    assert "drapp-booking-826" in detail.text
    assert "reserved" in detail.text
    assert "staff-826" in detail.text


def test_simulator_does_not_confirm_drapp_turno_when_provider_times_out(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant DrApp Timeout",
            "whatsapp:+828",
            ai_settings={
                "enabled": True,
                "tools_enabled": True,
                "availability_lookup_enabled": True,
                "max_offered_slots": 5,
                "require_confirmation_before_booking": True,
            },
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "DrApp Timeout",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="drapp",
            configuracion_externa={
                "drapp": {
                    "user": "timeout-user",
                    "password": "timeout-private-password",
                    "staff_id": "staff-828",
                    "days": 21,
                }
            },
        )
    )
    phone = "5491182800001"
    patient_id = asyncio.run(
        create_paciente(db_session, tenant_id, phone, obra_social="OSDE")
    )
    asyncio.run(
        create_user(
            db_session,
            "drapp-timeout@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    start = (now_ba() + timedelta(days=3)).replace(
        hour=11,
        minute=0,
        second=0,
        microsecond=0,
    )
    selection = SlotSelection(
        number=23,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        duration_minutes=30,
        timezone="America/Argentina/Buenos_Aires",
        label=f"{start.strftime('%d/%m')} 11:00",
    )

    def controlled_availability(tenant, consultorio, limit):
        return [selection]

    def timed_out_reservation(tenant, consultorio, selected, patient):
        raise TimeoutError("timeout-private-password")

    monkeypatch.setattr(
        "app.integrations.consultorio_movil.list_next_presential_slots",
        controlled_availability,
    )
    monkeypatch.setattr(
        "app.integrations.consultorio_movil.reserve_presential_slot",
        timed_out_reservation,
    )

    async def seed_confirmation_state():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            patient = await session.get(Paciente, patient_id)
            assert tenant is not None and consultorio is not None and patient is not None
            slots = await DrAppProvider().list_available_slots(
                tenant,
                consultorio,
                start,
                start + timedelta(days=1),
            )
            await ConversacionRepository(session).upsert_state(
                tenant_id,
                phone,
                ConversationState.ASK_AI_BOOKING_CONFIRMATION.value,
                {
                    "patient_id": patient_id,
                    "reason": "turno_presencial",
                    "for_whom": "self",
                    "first_time": False,
                    "appointment": {
                        "type": "presential",
                        "awaiting_booking_confirmation": True,
                        "selected_slot": {
                            "option": 1,
                            "label": selection.label,
                            "start_at": selection.start_at.isoformat(),
                            "end_at": selection.end_at.isoformat(),
                            "timezone": selection.timezone,
                            "provider": "drapp",
                            "metadata": {
                                "consultorio_id": consultorio_id,
                                "external_event_id": slots[0].slot_id,
                            },
                        },
                    },
                },
            )
            await session.commit()

    asyncio.run(seed_confirmation_state())
    login(client, "drapp-timeout@test.com", "secret-123")
    page = client.get("/admin/chat-simulator")
    response = client.post(
        "/admin/chat-simulator/api",
        headers={"X-CSRF-Token": _csrf(page.text)},
        json={
            "to_number": "whatsapp:+828",
            "from_number": f"whatsapp:+{phone}",
            "patient_id": patient_id,
            "message": "1",
        },
    )

    assert response.status_code == 200
    reply = response.json()["reply"]
    assert "No pude completar la reserva por un problema con la agenda" in reply
    assert "Turno confirmado" not in reply
    assert "timeout-private-password" not in reply

    async def load_turnos():
        async with db_session() as session:
            result = await session.execute(
                select(Turno).where(Turno.tenant_id == tenant_id)
            )
            return list(result.scalars().all())

    turnos = asyncio.run(load_turnos())
    assert len(turnos) == 1
    assert turnos[0].status == AppointmentStatus.CANCELLED
    assert not any(turno.status == AppointmentStatus.CONFIRMED for turno in turnos)
