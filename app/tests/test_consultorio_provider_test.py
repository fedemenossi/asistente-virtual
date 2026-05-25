from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from app.core.security import hash_password
from app.integrations.consultorio_movil import SlotSelection
from app.models.user import UserRole
from app.tests.conftest import create_consultorio, create_tenant, create_user, login


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("CSRF token no encontrado")
    return match.group(1)


def _seed_tenant_with_consultorio(client, db_session, *, email: str = "tenant-cabildo@test.com"):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cabildo UI", "whatsapp:+54110001"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Monroe",
            proveedor_turnos="consultorio_movil",
            configuracion_externa={
                "cabildo": {
                    "user": "saved-user",
                    "password": "saved-password",
                    "staff_id": "123",
                    "days": 21,
                }
            },
        )
    )
    asyncio.run(
        create_user(
            db_session,
            email,
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, email, "secret-123")
    return tenant_id, consultorio_id


def test_consultorio_edit_shows_provider_test_button(client, db_session):
    _, consultorio_id = _seed_tenant_with_consultorio(client, db_session)

    response = client.get(f"/t/consultorios/{consultorio_id}/edit")

    assert response.status_code == 200
    assert "Probar conexion" in response.text
    assert f"/t/consultorios/{consultorio_id}/test-provider" in response.text
    assert "proximos 3 dias" in response.text


def test_consultorio_provider_test_returns_slots_without_secrets(client, db_session, monkeypatch):
    _, consultorio_id = _seed_tenant_with_consultorio(client, db_session)
    page = client.get(f"/t/consultorios/{consultorio_id}/edit")
    csrf = _extract_csrf(page.text)
    captured = {}
    start = datetime(2026, 5, 25, 13, 30, tzinfo=timezone(timedelta(hours=-3)))

    def _fake_list_next_presential_slots(tenant, consultorio, limit):
        captured["tenant_id"] = tenant.id
        captured["limit"] = limit
        captured["cabildo"] = consultorio.configuracion_externa["cabildo"].copy()
        return [
            SlotSelection(
                number=1,
                start_at=start,
                end_at=start + timedelta(minutes=30),
                duration_minutes=30,
                timezone="America/Argentina/Buenos_Aires",
                label="Lunes 25/05 13:30",
            )
        ]

    monkeypatch.setattr(
        "app.web.tenant.views.list_next_presential_slots",
        _fake_list_next_presential_slots,
    )

    response = client.post(
        f"/t/consultorios/{consultorio_id}/test-provider",
        data={
            "csrf_token": csrf,
            "proveedor_turnos": "consultorio_movil",
            "cab_user": "form-user",
            "cab_password": "form-password",
            "cab_staff_id": "33920",
            "cab_days": "21",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["lookahead_days"] == 3
    assert payload["slots"][0]["label"] == "Lunes 25/05 13:30"
    assert captured["limit"] == 30
    assert captured["cabildo"]["user"] == "form-user"
    assert captured["cabildo"]["password"] == "form-password"
    assert captured["cabildo"]["staff_id"] == "33920"
    assert captured["cabildo"]["days"] == 3
    assert "form-password" not in response.text
    assert "saved-password" not in response.text


def test_consultorio_provider_test_rejects_non_cabildo_provider(client, db_session):
    _, consultorio_id = _seed_tenant_with_consultorio(client, db_session)
    page = client.get(f"/t/consultorios/{consultorio_id}/edit")
    csrf = _extract_csrf(page.text)

    response = client.post(
        f"/t/consultorios/{consultorio_id}/test-provider",
        data={"csrf_token": csrf, "proveedor_turnos": "google"},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_consultorio_provider_test_keeps_tenant_scope(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant A", "whatsapp:+54110002"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant B", "whatsapp:+54110003"))
    consultorio_b = asyncio.run(
        create_consultorio(
            db_session,
            tenant_b,
            "Monroe B",
            proveedor_turnos="consultorio_movil",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-a-scope@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    login(client, "tenant-a-scope@test.com", "secret-123")
    form = client.get("/t/consultorios/new")
    csrf = _extract_csrf(form.text)

    response = client.post(
        f"/t/consultorios/{consultorio_b}/test-provider",
        data={
            "csrf_token": csrf,
            "proveedor_turnos": "consultorio_movil",
            "cab_user": "u",
            "cab_password": "p",
            "cab_staff_id": "1",
        },
    )

    assert response.status_code == 404
