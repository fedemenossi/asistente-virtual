from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import pytest

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.integrations.consultorio_movil import SlotSelection
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


class _GoogleEventsRequest:
    def __init__(self, payload: dict | Exception):
        self._payload = payload

    def execute(self) -> dict:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _GoogleEvents:
    def __init__(self, payload: dict | Exception):
        self._payload = payload

    def list(self, **kwargs):
        return _GoogleEventsRequest(self._payload)


class _GoogleService:
    def __init__(self, payload: dict | Exception):
        self._payload = payload

    def events(self):
        return _GoogleEvents(self._payload)


@pytest.mark.parametrize("failed_provider", ["drapp", "google", "both"])
def test_simulator_degrades_availability_and_handoffs_total_failure(
    client,
    db_session,
    monkeypatch,
    failed_provider,
):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Partial DrApp",
            "whatsapp:+829",
            ai_settings={
                "enabled": True,
                "tools_enabled": True,
                "availability_lookup_enabled": True,
                "max_offered_slots": 5,
                "require_confirmation_before_booking": True,
            },
            calendar_settings={
                "google_credentials_json": "{}",
                "default_timezone": "America/Argentina/Buenos_Aires",
            },
        )
    )
    eligibility = {
        "accepts_first_consultation": True,
        "coverage_types": ["prepaga"],
        "coverage_entities": ["OSDE"],
        "weekly_ranges": [],
    }
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "DrApp Centro",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="drapp",
            configuracion_externa={
                "drapp": {
                    "user": "controlled-user",
                    "password": "drapp-private-detail",
                    "staff_id": "staff-829",
                    "days": 21,
                },
                "eligibility": eligibility,
            },
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Google Centro",
            tipo=TipoConsultorio.PRESENCIAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {"calendar_id": "google-829"},
                "eligibility": eligibility,
            },
        )
    )
    phone = "5491182900001"
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            nombre="Paula",
            apellido="Parcial",
            dni="40999111",
            health_coverage_type="prepaga",
            obra_social="OSDE",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "partial-drapp@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "partial-operator@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    start = (now_ba() + timedelta(days=2)).replace(
        hour=14,
        minute=30,
        second=0,
        microsecond=0,
    )
    google_payload = {
        "items": [
            {
                "id": "google-partial-829",
                "summary": "[DISPONIBLE] Consulta",
                "start": {
                    "dateTime": start.isoformat(),
                    "timeZone": "America/Argentina/Buenos_Aires",
                },
                "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
                "extendedProperties": {"private": {"slot_status": "available"}},
            }
        ]
    }

    def controlled_drapp_availability(tenant, consultorio, limit):
        if failed_provider in {"drapp", "both"}:
            raise TimeoutError("drapp-private-detail")
        drapp_start = start.replace(hour=13, minute=15)
        return [
            SlotSelection(
                number=19,
                start_at=drapp_start,
                end_at=drapp_start + timedelta(minutes=45),
                duration_minutes=45,
                timezone="America/Argentina/Buenos_Aires",
                label=f"{drapp_start.strftime('%d/%m')} 13:15",
            )
        ]

    monkeypatch.setattr(
        "app.integrations.consultorio_movil.list_next_presential_slots",
        controlled_drapp_availability,
    )
    monkeypatch.setattr(
        "app.integrations.google_calendar_provider.GoogleCalendarProvider._build_service",
        lambda self: _GoogleService(
            RuntimeError("google-private-detail")
            if failed_provider in {"google", "both"}
            else google_payload
        ),
    )
    login(client, "partial-drapp@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+829",
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

    assert "drapp-private-detail" not in offer
    assert "google-private-detail" not in offer
    if failed_provider != "both":
        assert ("14:30" if failed_provider == "drapp" else "13:15") in offer
        assert "parte de las agendas" in offer
        return

    assert "consultorio te contacte" in offer
    assert "Turno confirmado" not in offer

    login(client, "partial-operator@test.com", "secret-123")
    inbox = client.get("/t/conversation-states")

    assert inbox.status_code == 200
    assert "Falta de disponibilidad" in inbox.text
    assert "Paula Parcial" in inbox.text
    assert 'href="https://wa.me/5491182900001"' in inbox.text
    detail_match = re.search(
        r'href="(/t/conversation-states/5491182900001)"',
        inbox.text,
    )
    assert detail_match is not None
    detail = client.get(detail_match.group(1))

    assert detail.status_code == 200
    assert "Falta de disponibilidad" in detail.text
    assert "prepaga" in detail.text
    assert "OSDE" in detail.text
    assert "presential" in detail.text
    assert "first_consultation" in detail.text
    assert "rules_applied" in detail.text
    assert "sources_consulted" in detail.text
    assert "drapp" in detail.text
    assert "google" in detail.text
    assert "failed" in detail.text
    assert "drapp-private-detail" not in detail.text
    assert "google-private-detail" not in detail.text
