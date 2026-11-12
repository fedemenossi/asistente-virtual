from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.integrations.interfaces import CalendarSlot
from app.models.consultorio import TipoConsultorio
from app.models.user import UserRole
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


def _google_config() -> dict:
    schedule = {
        day: {
            "enabled": day == "monday",
            "start": "08:00",
            "end": "18:00",
            "slot_minutes": 30,
            "buffer_minutes": 0,
        }
        for day in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
    }
    return {
        "calendar_id": "calendar-rounds",
        "timezone": "America/Argentina/Buenos_Aires",
        "schedule": schedule,
    }


def test_operator_generates_all_configured_schedule_ranges(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Range Generation", "whatsapp:+822"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual por franja",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": _google_config(),
                "eligibility": {
                    "accepts_first_consultation": True,
                    "coverage_types": ["particular", "obra_social", "prepaga"],
                    "coverage_entities": [],
                    "weekly_ranges": [
                        {"day": "monday", "start": "10:00", "end": "11:00"},
                        {"day": "monday", "start": "15:00", "end": "16:00"},
                    ],
                },
            },
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "range-generation@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    generated = []

    async def capture_generation(self, tenant, consultorio, slots):
        generated.extend(slots)
        return {"created": len(slots), "skipped": 0}

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.generate_available_slots",
        capture_generation,
    )
    login(client, "range-generation@test.com", "secret-123")
    page = client.get(f"/t/consultorios/{consultorio_id}/calendar-slots")

    assert page.status_code == 200
    assert "todos los horarios semanales configurados" in page.text

    response = client.post(
        f"/t/consultorios/{consultorio_id}/calendar-slots",
        data={
            "csrf_token": _csrf(page.text),
            "date_from": "2026-11-09",
            "date_to": "2026-11-09",
            "action": "generate",
        },
    )

    assert response.status_code == 200
    assert [(slot.start_at.strftime("%H:%M"), slot.end_at.strftime("%H:%M")) for slot in generated] == [
        ("10:00", "10:30"),
        ("10:30", "11:00"),
        ("15:00", "15:30"),
        ("15:30", "16:00"),
    ]

    other_tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant Foreign Range", "whatsapp:+824")
    )
    asyncio.run(
        create_user(
            db_session,
            "foreign-range@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            other_tenant_id,
        )
    )
    login(client, "foreign-range@test.com", "secret-123")
    foreign_form = client.get("/t/consultorios/new")

    assert client.get(f"/t/consultorios/{consultorio_id}/calendar-slots").status_code == 404
    foreign_generation = client.post(
        f"/t/consultorios/{consultorio_id}/calendar-slots",
        data={
            "csrf_token": _csrf(foreign_form.text),
            "date_from": "2026-11-09",
            "date_to": "2026-11-09",
            "action": "generate",
        },
    )
    assert foreign_generation.status_code == 404
    assert len(generated) == 4


def test_simulator_combines_modalities_in_chronological_rounds_and_reserves_server_reference(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Availability Rounds",
            "whatsapp:+823",
            ai_settings={
                "enabled": True,
                "tools_enabled": True,
                "availability_lookup_enabled": True,
                "max_offered_slots": 5,
                "require_confirmation_before_booking": True,
            },
            calendar_settings={"default_timezone": "America/Argentina/Buenos_Aires"},
        )
    )
    eligibility = {
        "accepts_first_consultation": True,
        "coverage_types": ["prepaga"],
        "coverage_entities": ["OSDE"],
        "weekly_ranges": [],
    }
    virtual_a = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Norte",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {"calendar_id": "virtual-a"},
                "eligibility": eligibility,
            },
        )
    )
    virtual_b = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Sur",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {"calendar_id": "virtual-b"},
                "eligibility": eligibility,
            },
        )
    )
    presential = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Presencial Centro",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {"calendar_id": "presential"},
                "eligibility": eligibility,
            },
        )
    )
    phone = "5491182300001"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            nombre="Irene",
            apellido="Rondas",
            dni="40444555",
            health_coverage_type="prepaga",
            obra_social="OSDE",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "availability-rounds@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    base = now_ba().replace(hour=9, minute=0, second=0, microsecond=0)
    slots_by_consultorio = {
        virtual_a: [(1, 11, "event-v-a-1"), (4, 14, "event-v-a-4"), (7, 17, "event-v-a-7")],
        virtual_b: [(2, 12, "event-v-b-2"), (5, 15, "event-v-b-5")],
        presential: [(1, 10, "event-p-duplicate-day"), (3, 13, "event-p-3"), (6, 16, "event-p-6")],
    }

    async def available_slots(self, tenant, consultorio, window_start, window_end):
        return [
            CalendarSlot(
                slot_id=event_id,
                start_at=(base + timedelta(days=day_offset)).replace(hour=hour),
                end_at=(base + timedelta(days=day_offset)).replace(hour=hour) + timedelta(minutes=30),
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id=f"calendar-{consultorio.id}",
            )
            for day_offset, hour, event_id in slots_by_consultorio[consultorio.id]
        ]

    reservation_calls = []

    async def reserve_slot(self, tenant, consultorio, slot_id, patient, metadata):
        reservation_calls.append((consultorio.id, slot_id, patient.id))
        return {
            "event_id": slot_id,
            "calendar_id": f"calendar-{consultorio.id}",
            "start_at": (base + timedelta(days=6)).isoformat(),
            "end_at": (base + timedelta(days=6, minutes=30)).isoformat(),
            "timezone": "America/Argentina/Buenos_Aires",
            "html_link": "https://calendar.google.com/round-reserved",
            "meet_link": None,
        }

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.list_available_slots",
        available_slots,
    )
    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.reserve_slot",
        reserve_slot,
    )
    login(client, "availability-rounds@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+823",
                "from_number": f"whatsapp:+{phone}",
                "patient_id": patient_id,
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("1")
    send("Quiero un turno presencial o virtual, cualquiera de las dos modalidades")
    send("1")
    coverage_prompt = send("2")
    assert "1) OSDE (prepaga)" in coverage_prompt
    first_round = send("1")

    assert len(re.findall(r"^\d+\)", first_round, flags=re.MULTILINE)) == 5
    assert "(Presencial)" in first_round
    assert "(Virtual)" in first_round
    assert (
        first_round.index("10:00")
        < first_round.index("11:00")
        < first_round.index("12:00")
        < first_round.index("13:00")
        < first_round.index("14:00")
    )
    assert "15:00" not in first_round
    assert "16:00" not in first_round
    assert "17:00" not in first_round

    second_round = send("mas opciones")

    assert "16:00" in second_round
    assert "15:00" in second_round
    assert "17:00" in second_round
    assert "10:00" not in second_round
    assert "12:00" not in second_round
    assert "13:00" not in second_round
    assert "14:00" not in second_round
    assert "event-p-6" not in second_round

    assert "Confirmas que queres reservarlo" in send("2")
    confirmation = send("1")

    assert "Turno confirmado" in confirmation
    assert reservation_calls == [(presential, "event-p-6", patient_id)]
