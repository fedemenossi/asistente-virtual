from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.integrations.interfaces import CalendarSlot
from app.models.consultorio import TipoConsultorio
from app.models.user import UserRole
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_user, login


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_operator_configures_eligibility_and_multiple_generation_ranges(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Eligibility Form", "whatsapp:+816"))
    asyncio.run(
        create_user(
            db_session,
            "eligibility-form@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    monkeypatch.setattr(
        "app.web.tenant.views._load_google_calendars_for_tenant",
        lambda tenant: ([{"id": "eligibility-calendar", "summary": "Agenda", "access_role": "owner"}], None),
    )
    login(client, "eligibility-form@test.com", "secret-123")
    form = client.get("/t/consultorios/new")

    assert form.status_code == 200
    assert "Reglas de elegibilidad" in form.text
    assert "Admite primera consulta" in form.text
    assert "Prepagas admitidas para turnos" in form.text
    assert "Horarios para generar slots" in form.text
    assert "Restringir la oferta a estas franjas" not in form.text

    missing_prepaga = client.post(
        "/t/consultorios/new",
        data={
            "csrf_token": _csrf(form.text),
            "nombre": "Prepaga sin parametrizar",
            "tipo": "virtual",
            "proveedor_turnos": "",
            "eligibility_accepts_first_consultation": "1",
            "eligibility_coverage_types": ["prepaga"],
            "eligibility_prepaga_entities": "",
        },
    )
    assert missing_prepaga.status_code == 200
    assert "Indica al menos una prepaga admitida" in missing_prepaga.text

    response = client.post(
        "/t/consultorios/new",
        data={
            "csrf_token": _csrf(form.text),
            "nombre": "Caja Notarial Centro",
            "tipo": "presencial",
            "proveedor_turnos": "google_calendar",
            "gcal_calendar_id": "eligibility-calendar",
            "gcal_timezone": "America/Argentina/Buenos_Aires",
            "gcal_available_tag": "[TURNO DISPONIBLE]",
            "gcal_reserved_tag_template": "[TURNO {patient_full_name}]",
            "eligibility_accepts_first_consultation": "1",
            "eligibility_coverage_types": ["obra_social", "prepaga"],
            "eligibility_coverage_entities": "Caja Notarial",
            "eligibility_prepaga_entities": "OSDE\nSwiss Medical",
            "gcal_schedule_mode": "ranges",
            "gcal_schedule_day": ["monday", "wednesday"],
            "gcal_schedule_start": ["09:00", "14:00"],
            "gcal_schedule_end": ["12:00", "18:00"],
            "gcal_schedule_slot_minutes": ["30", "45"],
            "gcal_schedule_buffer_minutes": ["0", "15"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    listing = client.get(response.headers["location"])
    edit_url_match = re.search(r'href="(/t/consultorios/\d+/edit)"', listing.text)
    assert edit_url_match is not None
    edit = client.get(edit_url_match.group(1))
    assert edit.status_code == 200
    assert 'name="eligibility_accepts_first_consultation" value="1" checked' in edit.text
    assert 'name="eligibility_coverage_types" value="obra_social" checked' in edit.text
    assert 'name="eligibility_coverage_types" value="prepaga" checked' in edit.text
    assert 'name="eligibility_coverage_entities"' in edit.text
    assert ">Caja Notarial</textarea>" in edit.text
    assert 'name="eligibility_prepaga_entities"' in edit.text
    assert "OSDE\nSwiss Medical</textarea>" in edit.text
    assert '<option value="monday" selected>' in edit.text
    assert 'name="gcal_schedule_start" value="09:00"' in edit.text
    assert 'name="gcal_schedule_end" value="12:00"' in edit.text
    assert '<option value="wednesday" selected>' in edit.text
    assert 'name="gcal_schedule_start" value="14:00"' in edit.text
    assert 'name="gcal_schedule_end" value="18:00"' in edit.text
    assert 'name="gcal_schedule_slot_minutes" value="45"' in edit.text
    assert 'name="gcal_schedule_buffer_minutes" value="15"' in edit.text

    edited = client.post(
        edit_url_match.group(1),
        data={
            "csrf_token": _csrf(edit.text),
            "nombre": "Particular Centro",
            "tipo": "virtual",
            "proveedor_turnos": "google_calendar",
            "gcal_calendar_id": "eligibility-calendar",
            "gcal_timezone": "America/Argentina/Buenos_Aires",
            "gcal_available_tag": "[TURNO DISPONIBLE]",
            "gcal_reserved_tag_template": "[TURNO {patient_full_name}]",
            "eligibility_coverage_types": ["particular"],
            "eligibility_coverage_entities": "",
            "gcal_schedule_mode": "ranges",
            "gcal_schedule_day": ["tuesday"],
            "gcal_schedule_start": ["08:00"],
            "gcal_schedule_end": ["11:00"],
            "gcal_schedule_slot_minutes": ["20"],
            "gcal_schedule_buffer_minutes": ["5"],
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    reopened = client.get(edited.headers["location"])
    assert 'name="eligibility_accepts_first_consultation" value="1" checked' not in reopened.text
    assert 'name="eligibility_coverage_types" value="particular" checked' in reopened.text
    assert '<option value="tuesday" selected>' in reopened.text
    assert 'name="gcal_schedule_start" value="08:00"' in reopened.text
    assert 'name="gcal_schedule_slot_minutes" value="20"' in reopened.text
    assert 'name="gcal_schedule_buffer_minutes" value="5"' in reopened.text

    other_tenant_id = asyncio.run(create_tenant(db_session, "Tenant Eligibility Other", "whatsapp:+821"))
    asyncio.run(
        create_user(
            db_session,
            "eligibility-other@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            other_tenant_id,
        )
    )
    login(client, "eligibility-other@test.com", "secret-123")
    other_form = client.get("/t/consultorios/new")
    assert client.get(edit_url_match.group(1)).status_code == 404
    cross_tenant_update = client.post(
        edit_url_match.group(1),
        data={
            "csrf_token": _csrf(other_form.text),
            "nombre": "No autorizado",
            "tipo": "virtual",
            "proveedor_turnos": "",
            "eligibility_accepts_first_consultation": "1",
            "eligibility_coverage_types": ["particular"],
        },
        follow_redirects=False,
    )
    assert cross_tenant_update.status_code == 404


def test_simulator_offers_only_consultorios_eligible_for_first_consultation_and_coverage(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Eligibility Offer",
            "whatsapp:+817",
            ai_settings={"enabled": True, "tools_enabled": True, "availability_lookup_enabled": True},
            calendar_settings={"default_timezone": "America/Argentina/Buenos_Aires"},
        )
    )
    base_config = {
        "google_calendar": {"calendar_id": "calendar"},
        "eligibility": {
            "accepts_first_consultation": True,
            "coverage_types": ["prepaga"],
            "coverage_entities": ["OSDE"],
            "weekly_ranges": [],
        },
    }
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual sin primeras",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                **base_config,
                "eligibility": {**base_config["eligibility"], "accepts_first_consultation": False},
            },
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Caja",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                **base_config,
                "eligibility": {**base_config["eligibility"], "coverage_entities": ["Caja Notarial"]},
            },
        )
    )
    eligible_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual OSDE",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa=base_config,
        )
    )
    phone = "5491181700001"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            dni="40111222",
            obra_social="OSDE",
            health_coverage_type="prepaga",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "eligibility-offer@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    start = (now_ba() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)

    async def available_slot(self, tenant, consultorio, window_start, window_end):
        hour = 10 if consultorio.id == eligible_id else 15
        slot_start = start.replace(hour=hour)
        return [
            CalendarSlot(
                slot_id=f"slot-{consultorio.id}",
                start_at=slot_start,
                end_at=slot_start + timedelta(minutes=30),
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id=f"calendar-{consultorio.id}",
            )
        ]

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.list_available_slots",
        available_slot,
    )
    login(client, "eligibility-offer@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+817",
                "from_number": f"whatsapp:+{phone}",
                "patient_id": patient_id,
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("1")
    send("2")
    send("1")
    coverage_prompt = send("1")
    assert "1) Caja Notarial" in coverage_prompt
    assert "2) OSDE" in coverage_prompt
    offer = send("2")

    assert "turnos virtuales disponibles" in offer
    assert "10:00" in offer
    assert "15:00" not in offer


def test_simulator_keeps_reimbursement_patient_particular_and_filters_modality_tenant_and_ranges(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Particular Offer",
            "whatsapp:+818",
            ai_settings={"enabled": True, "tools_enabled": True, "availability_lookup_enabled": True},
        )
    )
    other_tenant_id = asyncio.run(create_tenant(db_session, "Tenant Other Offer", "whatsapp:+819"))
    start = (now_ba() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    day_key = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[
        start.weekday()
    ]
    particular_config = {
        "google_calendar": {"calendar_id": "particular-calendar"},
        "eligibility": {
            "accepts_first_consultation": True,
            "coverage_types": ["particular"],
            "coverage_entities": [],
            "weekly_ranges": [{"day": day_key, "start": "14:00", "end": "16:00"}],
        },
    }
    particular_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Particular",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa=particular_config,
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Caja Notarial",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {"calendar_id": "caja-calendar"},
                "eligibility": {
                    "accepts_first_consultation": True,
                    "coverage_types": ["obra_social"],
                    "coverage_entities": ["Caja Notarial"],
                    "weekly_ranges": [],
                },
            },
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Presencial Particular",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="google",
            configuracion_externa=particular_config,
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            other_tenant_id,
            "Virtual Tenant Ajeno",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa=particular_config,
        )
    )
    phone = "5491181800001"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            dni="40222333",
            health_coverage_type="particular",
            obra_social="OSDE",
            insurance_number="REINTEGRO-1",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "particular-offer@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )

    async def available_slots(self, tenant, consultorio, window_start, window_end):
        if consultorio.id != particular_id:
            slot_start = start.replace(hour=11)
            return [
                CalendarSlot(
                    slot_id=f"unexpected-{consultorio.id}",
                    start_at=slot_start,
                    end_at=slot_start + timedelta(minutes=30),
                    timezone="America/Argentina/Buenos_Aires",
                    provider="google_calendar",
                    calendar_id="unexpected-calendar",
                )
            ]
        return [
            CalendarSlot(
                slot_id="outside-range",
                start_at=start.replace(hour=10),
                end_at=start.replace(hour=10, minute=30),
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id="particular-calendar",
            ),
            CalendarSlot(
                slot_id="inside-range",
                start_at=start.replace(hour=15),
                end_at=start.replace(hour=15, minute=30),
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id="particular-calendar",
            ),
            CalendarSlot(
                slot_id="crosses-range-end",
                start_at=start.replace(hour=15, minute=50),
                end_at=start.replace(hour=16, minute=20),
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id="particular-calendar",
            ),
        ]

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.list_available_slots",
        available_slots,
    )
    login(client, "particular-offer@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+818",
                "from_number": f"whatsapp:+{phone}",
                "patient_id": patient_id,
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("1")
    send("2")
    send("1")
    coverage_prompt = send("2")
    assert "1) Particular" in coverage_prompt
    assert "2) Caja Notarial" in coverage_prompt
    offer = send("1")

    assert "turnos virtuales disponibles" in offer
    assert "15:00" in offer
    assert "10:00" not in offer
    assert "11:00" not in offer
    assert "15:50" not in offer


def test_simulator_prefers_specific_coverage_consultorio_for_caja_notarial(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Caja Offer",
            "whatsapp:+820",
            ai_settings={"enabled": True, "tools_enabled": True, "availability_lookup_enabled": True},
        )
    )
    generic_config = {
        "google_calendar": {"calendar_id": "generic-calendar"},
        "eligibility": {
            "accepts_first_consultation": True,
            "coverage_types": ["obra_social"],
            "coverage_entities": [],
            "weekly_ranges": [],
        },
    }
    generic_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "General Obras Sociales",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa=generic_config,
        )
    )
    caja_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Exclusivo Caja Notarial",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {"calendar_id": "caja-calendar"},
                "eligibility": {
                    **generic_config["eligibility"],
                    "coverage_entities": ["Caja Notarial"],
                },
            },
        )
    )
    phone = "5491182000001"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            dni="40333444",
            health_coverage_type="obra_social",
            obra_social="Caja Notarial",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "caja-offer@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    start = (now_ba() + timedelta(days=4)).replace(minute=0, second=0, microsecond=0)

    async def available_slots(self, tenant, consultorio, window_start, window_end):
        hour = 9 if consultorio.id == generic_id else 10
        slot_start = start.replace(hour=hour)
        return [
            CalendarSlot(
                slot_id=f"slot-{consultorio.id}",
                start_at=slot_start,
                end_at=slot_start + timedelta(minutes=30),
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id=f"calendar-{consultorio.id}",
            )
        ]

    monkeypatch.setattr(
        "app.services.calendar_service.CalendarService.list_available_slots",
        available_slots,
    )
    login(client, "caja-offer@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+820",
                "from_number": f"whatsapp:+{phone}",
                "patient_id": patient_id,
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("1")
    send("2")
    send("1")
    coverage_prompt = send("2")
    assert "1) Caja Notarial (obra social)" in coverage_prompt
    offer = send("1")

    assert caja_id != generic_id
    assert "10:00" in offer
    assert "09:00" not in offer
