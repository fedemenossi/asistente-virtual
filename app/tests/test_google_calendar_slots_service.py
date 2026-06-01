from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from app.core.security import hash_password
from app.integrations.google_calendar_provider import GoogleCalendarProvider
from app.models.consultorio import TipoConsultorio
from app.models.user import UserRole
from app.services.calendar_service import CalendarService
from app.services.google_calendar_slots_service import (
    calculate_slots,
    validate_google_calendar_config,
)
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_user, login


def _base_config(**day_overrides):
    schedule = {
        "monday": {"enabled": False, "start": "09:00", "end": "17:00", "slot_minutes": 30, "buffer_minutes": 0},
        "tuesday": {"enabled": False, "start": "09:00", "end": "17:00", "slot_minutes": 30, "buffer_minutes": 0},
        "wednesday": {"enabled": False, "start": "09:00", "end": "17:00", "slot_minutes": 30, "buffer_minutes": 0},
        "thursday": {"enabled": False, "start": "09:00", "end": "17:00", "slot_minutes": 30, "buffer_minutes": 0},
        "friday": {"enabled": False, "start": "09:00", "end": "17:00", "slot_minutes": 30, "buffer_minutes": 0},
        "saturday": {"enabled": False, "start": "09:00", "end": "17:00", "slot_minutes": 30, "buffer_minutes": 0},
        "sunday": {"enabled": False, "start": "09:00", "end": "17:00", "slot_minutes": 30, "buffer_minutes": 0},
    }
    for key, value in day_overrides.items():
        schedule[key].update(value)
    return {
        "calendar_id": "calendar-1",
        "timezone": "America/Argentina/Buenos_Aires",
        "schedule": schedule,
    }


def test_calculate_slots_with_buffer_and_end_boundary():
    config = _base_config(
        monday={"enabled": True, "start": "09:00", "end": "11:00", "slot_minutes": 30, "buffer_minutes": 10}
    )

    slots = calculate_slots(config, date(2026, 5, 4), date(2026, 5, 4))

    assert [(s.start_at.strftime("%H:%M"), s.end_at.strftime("%H:%M")) for s in slots] == [
        ("09:00", "09:30"),
        ("09:40", "10:10"),
        ("10:20", "10:50"),
    ]


def test_calculate_slots_excludes_inactive_days_and_holidays():
    config = _base_config(
        monday={"enabled": True, "start": "09:00", "end": "10:00", "slot_minutes": 30, "buffer_minutes": 0},
        tuesday={"enabled": False},
    )

    without_holidays = calculate_slots(config, date(2026, 5, 25), date(2026, 5, 26))
    with_holidays = calculate_slots(config, date(2026, 5, 25), date(2026, 5, 26), exclude_argentina_holidays=True)

    assert len(without_holidays) == 2
    assert with_holidays == []


def test_validate_google_calendar_config_rejects_invalid_day_range():
    config = _base_config(monday={"enabled": True, "start": "11:00", "end": "09:00"})

    try:
        validate_google_calendar_config(config)
    except ValueError as exc:
        assert "inicio debe ser menor" in str(exc)
    else:
        raise AssertionError("configuracion invalida aceptada")


def test_consultorio_form_renders_and_saves_google_calendar_config(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Google Form",
            "whatsapp:+9401",
            calendar_settings={"google_credentials_json": '{"client_email":"svc-calendar@example.com"}'},
        )
    )
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Virtual"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-google-form@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    monkeypatch.setattr(
        "app.web.tenant.views._load_google_calendars_for_tenant",
        lambda tenant: ([{"id": "cal-1", "summary": "Agenda Virtual", "access_role": "owner"}], None),
    )
    login(client, "tenant-google-form@test.com", "secret-123")
    page = client.get(f"/t/consultorios/{consultorio_id}/edit")
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

    assert "Google Calendar" in page.text
    assert "gcal_calendar_id" in page.text
    assert "ID de calendario de este consultorio" in page.text
    assert "Cada consultorio puede usar un calendario distinto" in page.text
    assert "Calendario cargado en settings" not in page.text
    assert "svc-calendar@example.com" in page.text

    response = client.post(
        f"/t/consultorios/{consultorio_id}/edit",
        data={
            "csrf_token": csrf,
            "nombre": "Virtual",
            "tipo": "virtual",
            "proveedor_turnos": "google_calendar",
            "gcal_calendar_id": "cal-1",
            "gcal_timezone": "America/Argentina/Buenos_Aires",
            "gcal_available_tag": "[TURNO DISPONIBLE]",
            "gcal_reserved_tag_template": "[TURNO {patient_full_name}]",
            "gcal_monday_enabled": "1",
            "gcal_monday_start": "09:00",
            "gcal_monday_end": "11:00",
            "gcal_monday_slot_minutes": "30",
            "gcal_monday_buffer_minutes": "10",
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/t/consultorios/{consultorio_id}/edit"

    async def _fetch():
        from app.models.consultorio import Consultorio

        async with db_session() as session:
            return await session.get(Consultorio, consultorio_id)

    consultorio = asyncio.run(_fetch())
    cfg = consultorio.configuracion_externa["google_calendar"]
    assert consultorio.proveedor_turnos == "google"
    assert cfg["calendar_id"] == "cal-1"
    assert cfg["schedule"]["monday"]["buffer_minutes"] == 10

    saved_page = client.get(f"/t/consultorios/{consultorio_id}/edit")
    assert "Consultorio actualizado" in saved_page.text


def test_new_consultorio_form_can_refresh_google_calendars_before_save(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Google New Form",
            "whatsapp:+9407",
            calendar_settings={"google_credentials_json": '{"client_email":"svc-calendar@example.com"}'},
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-google-new-form@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    monkeypatch.setattr(
        "app.web.tenant.views._load_google_calendars_for_tenant",
        lambda tenant: ([], None),
    )
    login(client, "tenant-google-new-form@test.com", "secret-123")

    page = client.get("/t/consultorios/new")

    assert page.status_code == 200
    assert 'data-google-calendars-url="/t/google-calendars"' in page.text
    assert "Para calendarios secundarios, pega aca el ID y presiona Actualizar calendarios" in page.text


def test_calendar_slots_page_renders_progress_indicator(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Slots Progress", "whatsapp:+9403"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Progress",
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": _base_config(monday={"enabled": True})},
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-slots-progress@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-slots-progress@test.com", "secret-123")

    response = client.get(f"/t/consultorios/{consultorio_id}/calendar-slots")

    assert response.status_code == 200
    assert "data-calendar-slots-progress" in response.text
    assert 'data-calendar-slots-action-input' in response.text
    assert "Consultando Google Calendar" in response.text
    assert "Generando..." in response.text


class _FakeCall:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _FakeEvents:
    def __init__(self, store):
        self.store = store

    def list(self, **kwargs):
        return _FakeCall({"items": list(self.store)})

    def insert(self, calendarId, body):
        body = {**body, "id": f"evt-{len(self.store) + 1}"}
        self.store.append(body)
        return _FakeCall(body)

    def get(self, calendarId, eventId):
        event = next(item for item in self.store if item["id"] == eventId)
        return _FakeCall(event)

    def patch(self, calendarId, eventId, body, **kwargs):
        event = next(item for item in self.store if item["id"] == eventId)
        event.update(body)
        return _FakeCall(event)


class _FakeService:
    def __init__(self, store):
        self.store = store

    def events(self):
        return _FakeEvents(self.store)


class _FakeCalendarList:
    def list(self, **kwargs):
        return _FakeCall({"items": []})

    def insert(self, body):
        calendar_id = body["id"]
        return _FakeCall({"id": calendar_id, "summary": "Calendario insertado", "accessRole": "writer"})


class _FakeCalendars:
    def get(self, calendarId):
        return _FakeCall({"id": calendarId, "summary": "Calendario directo"})


class _FakeCalendarService:
    def calendarList(self):
        return _FakeCalendarList()

    def calendars(self):
        return _FakeCalendars()


def _event(event_id, start, end, *, status="available", summary="[TURNO DISPONIBLE]"):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Argentina/Buenos_Aires"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Argentina/Buenos_Aires"},
        "extendedProperties": {
            "private": {
                "generated_by_app": "true",
                "tenant_id": "1",
                "consultorio_id": "1",
                "slot_status": status,
            }
        },
    }


def test_google_provider_deduplicates_and_reserves_slot(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Provider", "whatsapp:+9402"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Provider",
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": _base_config(monday={"enabled": True})},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+94021", nombre="Ana", apellido="Gomez"))

    async def _load():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant

        async with db_session() as session:
            return (
                await session.get(Tenant, tenant_id),
                await session.get(Consultorio, consultorio_id),
                await session.get(Paciente, paciente_id),
            )

    tenant, consultorio, paciente = asyncio.run(_load())
    start = datetime(2026, 5, 4, 9, 0, tzinfo=timezone(timedelta(hours=-3)))
    existing = [_event("evt-1", start, start + timedelta(minutes=30))]
    provider = GoogleCalendarProvider("cal-1", "{}")
    monkeypatch.setattr(provider, "_build_service", lambda: _FakeService(existing))

    slots = calculate_slots(_base_config(monday={"enabled": True, "start": "09:00", "end": "10:00"}), date(2026, 5, 4), date(2026, 5, 4))
    result = provider.generate_available_slots(tenant, consultorio, slots)

    assert result["duplicates"] == 1
    assert result["created"] == 1

    reserve = asyncio.run(provider.reserve_slot(tenant, consultorio, "evt-1", paciente, {"turno_id": 1}))
    assert reserve["event_id"] == "evt-1"
    assert existing[0]["extendedProperties"]["private"]["slot_status"] == "reserved"
    assert "Ana Gomez" in existing[0]["summary"]

    try:
        asyncio.run(provider.reserve_slot(tenant, consultorio, "evt-1", paciente, {}))
    except RuntimeError:
        pass
    else:
        raise AssertionError("slot reservado aceptado de nuevo")


def test_google_provider_adds_meet_only_for_virtual_consultorio(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Meet", "whatsapp:+9406"))
    virtual_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Meet",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": _base_config(monday={"enabled": True})},
        )
    )
    presential_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Presencial Meet",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": _base_config(monday={"enabled": True})},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+94061", nombre="Ana", apellido="Gomez"))

    async def _load():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant

        async with db_session() as session:
            return (
                await session.get(Tenant, tenant_id),
                await session.get(Consultorio, virtual_id),
                await session.get(Consultorio, presential_id),
                await session.get(Paciente, paciente_id),
            )

    tenant, virtual, presential, paciente = asyncio.run(_load())
    start = datetime(2026, 5, 4, 9, 0, tzinfo=timezone(timedelta(hours=-3)))
    virtual_event = [_event("evt-virtual", start, start + timedelta(minutes=30))]
    presential_event = [_event("evt-presential", start, start + timedelta(minutes=30))]

    virtual_provider = GoogleCalendarProvider("cal-virtual", "{}")
    monkeypatch.setattr(virtual_provider, "_build_service", lambda: _FakeService(virtual_event))
    asyncio.run(virtual_provider.reserve_slot(tenant, virtual, "evt-virtual", paciente, {}))
    assert "conferenceData" in virtual_event[0]

    presential_provider = GoogleCalendarProvider("cal-presential", "{}")
    monkeypatch.setattr(presential_provider, "_build_service", lambda: _FakeService(presential_event))
    asyncio.run(presential_provider.reserve_slot(tenant, presential, "evt-presential", paciente, {}))
    assert "conferenceData" not in presential_event[0]


def test_google_provider_registers_direct_calendar_when_calendar_list_is_empty(monkeypatch):
    provider = GoogleCalendarProvider("direct-calendar@example.com", "{}")
    monkeypatch.setattr(provider, "_build_service", lambda: _FakeCalendarService())

    calendars = provider.list_calendars()

    assert calendars == [
        {
            "id": "direct-calendar@example.com",
            "summary": "Calendario insertado",
            "access_role": "writer",
        }
    ]


def test_google_provider_lists_candidate_calendar_from_form(monkeypatch):
    provider = GoogleCalendarProvider("primary", "{}")
    monkeypatch.setattr(provider, "_build_service", lambda: _FakeCalendarService())

    calendars = provider.list_calendars("secondary-calendar@example.com")

    assert calendars == [
        {
            "id": "secondary-calendar@example.com",
            "summary": "Calendario insertado",
            "access_role": "writer",
        }
    ]


def test_calendar_service_does_not_use_legacy_fallback_when_listing_google_calendars(db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Calendar List",
            "whatsapp:+9406",
            calendar_settings={
                "google_credentials_json": "{}",
                "google_calendar_id": "legacy-calendar@example.com",
            },
        )
    )

    async def _load_tenant():
        from app.models.tenant import Tenant

        async with db_session() as session:
            return await session.get(Tenant, tenant_id)

    tenant = asyncio.run(_load_tenant())
    captured = {}

    def _fake_list(self, candidate_calendar_id=None):
        captured["calendar_id"] = self._calendar_id
        captured["candidate_calendar_id"] = candidate_calendar_id
        return []

    monkeypatch.setattr(GoogleCalendarProvider, "list_calendars", _fake_list)

    calendars = CalendarService().list_google_calendars(tenant)

    assert calendars == []
    assert captured == {"calendar_id": "primary", "candidate_calendar_id": None}
