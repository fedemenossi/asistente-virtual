from __future__ import annotations

import asyncio
import re

import pytest

from app.core.security import hash_password
from app.models.user import UserRole
from app.services.billing_fiscal_profile_service import normalize_receiver_iva_condition
from app.tests.conftest import create_paciente, create_tenant, create_user, login


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token no encontrado"
    return match.group(1)


def _patient_data(csrf_token: str, **overrides) -> dict[str, str]:
    data = {
        "csrf_token": csrf_token,
        "nombre": "Ana",
        "apellido": "Garcia",
        "telefono": "011 4555 1234",
        "dni": "27654321",
        "email": "ana@example.com",
        "tipo_documento": "DNI",
        "numero_documento": "27654321",
        "iva_condition": "consumidor_final",
    }
    data.update(overrides)
    return data


def test_patient_iva_condition_normalization():
    assert normalize_receiver_iva_condition(" Responsable_Inscripto ") == "responsable_inscripto"
    assert normalize_receiver_iva_condition("") is None
    with pytest.raises(ValueError, match="IVA"):
        normalize_receiver_iva_condition("invalida")


def test_tenant_admin_saves_and_updates_patient_iva_condition(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient Fiscal", "whatsapp:+549110000401"))
    asyncio.run(
        create_user(
            db_session,
            "patient-fiscal@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "patient-fiscal@example.com", "secret-123")

    form = client.get("/t/pacientes/new")
    assert "Condicion frente al IVA" in form.text
    response = client.post(
        "/t/pacientes/new",
        data=_patient_data(_csrf(form.text)),
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch_patient():
        from sqlalchemy import select

        from app.models.paciente import Paciente

        async with db_session() as session:
            return await session.scalar(
                select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.dni == "27654321")
            )

    patient = asyncio.run(_fetch_patient())
    assert patient is not None
    assert patient.iva_condition == "consumidor_final"

    edit = client.get(f"/t/pacientes/{patient.id}/edit")
    assert 'value="consumidor_final" selected' in edit.text
    response = client.post(
        f"/t/pacientes/{patient.id}/edit",
        data=_patient_data(_csrf(edit.text), iva_condition="exento"),
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert asyncio.run(_fetch_patient()).iva_condition == "exento"


def test_patient_iva_condition_is_tenant_scoped(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Patient A", "whatsapp:+549110000402"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Patient B", "whatsapp:+549110000403"))
    patient_a = asyncio.run(
        create_paciente(
            db_session,
            tenant_a,
            "whatsapp:+549110000404",
            dni="30123456",
            iva_condition="responsable_inscripto",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "patient-a@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "patient-b@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_b,
        )
    )
    login(client, "patient-a@example.com", "secret-123")
    own_page = client.get(f"/t/pacientes/{patient_a}/edit")
    assert own_page.status_code == 200
    assert 'value="responsable_inscripto" selected' in own_page.text

    client.get("/logout")
    login(client, "patient-b@example.com", "secret-123")
    assert client.get(f"/t/pacientes/{patient_a}/edit").status_code == 404
