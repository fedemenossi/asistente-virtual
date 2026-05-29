from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.security import hash_password
from app.models.user import UserRole
from app.tests.conftest import (
    create_consultorio,
    create_paciente,
    create_payment,
    create_tenant,
    create_turno,
    create_user,
    get_audit_logs,
    get_tenant,
    login,
)


def _setup_two_tenants(db_session):
    tenant_1 = asyncio.run(create_tenant(db_session, "Tenant Uno", "whatsapp:+111"))
    tenant_2 = asyncio.run(create_tenant(db_session, "Tenant Dos", "whatsapp:+222"))

    consultorio_1 = asyncio.run(create_consultorio(db_session, tenant_1, "Sede Uno"))
    consultorio_2 = asyncio.run(create_consultorio(db_session, tenant_2, "Sede Dos"))

    paciente_1 = asyncio.run(create_paciente(db_session, tenant_1, "whatsapp:+1111"))
    paciente_2 = asyncio.run(create_paciente(db_session, tenant_2, "whatsapp:+2222"))

    turno_1 = asyncio.run(create_turno(db_session, paciente_1, consultorio_1))
    turno_2 = asyncio.run(create_turno(db_session, paciente_2, consultorio_2))

    pago_1 = asyncio.run(create_payment(db_session, tenant_1, paciente_1, turno_1))
    pago_2 = asyncio.run(create_payment(db_session, tenant_2, paciente_2, turno_2))

    return {
        "tenant_1": tenant_1,
        "tenant_2": tenant_2,
        "turno_1": turno_1,
        "turno_2": turno_2,
        "pago_1": pago_1,
        "pago_2": pago_2,
    }


def _create_users(db_session, tenant_1: int, tenant_2: int):
    password_hash = hash_password("secret-123")
    admin_id = asyncio.run(
        create_user(db_session, "admin@test.com", password_hash, UserRole.SUPER_ADMIN.value, None)
    )
    tenant_1_id = asyncio.run(
        create_user(db_session, "tenant1@test.com", password_hash, UserRole.TENANT_ADMIN.value, tenant_1)
    )
    tenant_2_id = asyncio.run(
        create_user(db_session, "tenant2@test.com", password_hash, UserRole.TENANT_ADMIN.value, tenant_2)
    )
    return admin_id, tenant_1_id, tenant_2_id


def test_tenant_payments_are_isolated(client, db_session):
    data = _setup_two_tenants(db_session)
    _create_users(db_session, data["tenant_1"], data["tenant_2"])

    login(client, "tenant1@test.com", "secret-123")
    response = client.get("/t/payments")
    assert response.status_code == 200
    assert f"#{data['pago_1']}" in response.text
    assert f"#{data['pago_2']}" not in response.text

    detail = client.get(f"/t/payments/{data['pago_2']}")
    assert detail.status_code == 404


def test_tenant_appointments_are_isolated(client, db_session):
    data = _setup_two_tenants(db_session)
    _create_users(db_session, data["tenant_1"], data["tenant_2"])

    login(client, "tenant1@test.com", "secret-123")
    response = client.get("/t/appointments")
    assert response.status_code == 200
    assert f"/t/appointments/{data['turno_1']}" in response.text
    assert f"/t/appointments/{data['turno_2']}" not in response.text

    detail = client.get(f"/t/appointments/{data['turno_2']}")
    assert detail.status_code == 404


def test_turno_keeps_direct_tenant_id(client, db_session):
    data = _setup_two_tenants(db_session)
    _create_users(db_session, data["tenant_1"], data["tenant_2"])

    async def _fetch():
        from app.models.turno import Turno

        async with db_session() as session:
            turno_1 = await session.get(Turno, data["turno_1"])
            turno_2 = await session.get(Turno, data["turno_2"])
            return turno_1, turno_2

    turno_1, turno_2 = asyncio.run(_fetch())
    assert turno_1 is not None and turno_1.tenant_id == data["tenant_1"]
    assert turno_2 is not None and turno_2.tenant_id == data["tenant_2"]


def test_super_admin_sees_all_payments_and_appointments(client, db_session):
    data = _setup_two_tenants(db_session)
    _create_users(db_session, data["tenant_1"], data["tenant_2"])

    login(client, "admin@test.com", "secret-123")
    response = client.get("/admin/payments")
    assert response.status_code == 200
    assert f"#{data['pago_1']}" in response.text
    assert f"#{data['pago_2']}" in response.text

    response = client.get("/admin/appointments")
    assert response.status_code == 200
    assert f"#{data['turno_1']}" in response.text
    assert f"#{data['turno_2']}" in response.text


def test_tenant_profile_settings_update_and_audit(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Perfil", "whatsapp:+333"))
    admin_id, tenant_user_id, _ = _create_users(db_session, tenant_id, tenant_id)
    _ = admin_id
    _ = tenant_user_id

    async def _set_cuil():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.cuil = "20999888777"

    asyncio.run(_set_cuil())

    login(client, "tenant1@test.com", "secret-123")
    response = client.get("/t/settings")
    assert response.status_code == 200
    csrf_token = response.text.split('name="csrf_token" value="')[1].split('"')[0]

    payload = {
        "csrf_token": csrf_token,
        "nombre": "Tenant Perfil Editado",
        "fantasy_name": "Clinica Norte",
        "first_name": "Ana",
        "last_name": "Gomez",
        "address": "Calle 123",
        "postal_code": "1000",
        "phone": "1122334455",
        "whatsapp_number": "whatsapp:+333",
        "cuil": "20123456789",
    }
    result = client.post("/t/settings", data=payload, follow_redirects=False)
    assert result.status_code in (302, 303)

    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    assert tenant.nombre == "Tenant Perfil Editado"
    assert tenant.fantasy_name == "Clinica Norte"
    assert tenant.first_name == "Ana"
    assert tenant.last_name == "Gomez"
    assert tenant.address == "Calle 123"
    assert tenant.postal_code == "1000"
    assert tenant.phone == "1122334455"
    assert tenant.cuil == "20999888777"

    audit_logs = asyncio.run(get_audit_logs(db_session, "tenant"))
    profile_logs = [log for log in audit_logs if log.action == "update_profile"]
    assert profile_logs
    assert "nombre" in (profile_logs[-1].metadata_json or {})


def test_admin_can_create_tenant_with_profile_fields(client, db_session):
    asyncio.run(create_tenant(db_session, "Tenant Base", "whatsapp:+990"))
    password_hash = hash_password("secret-123")
    asyncio.run(
        create_user(db_session, "admin@test.com", password_hash, UserRole.SUPER_ADMIN.value, None)
    )

    login(client, "admin@test.com", "secret-123")
    response = client.get("/admin/tenants/new")
    assert response.status_code == 200
    csrf_token = response.text.split('name="csrf_token" value="')[1].split('"')[0]

    payload = {
        "csrf_token": csrf_token,
        "nombre": "Tenant Nuevo",
        "whatsapp_number": "whatsapp:+444",
        "fantasy_name": "Consultorio Delta",
        "first_name": "Luis",
        "last_name": "Martinez",
        "cuil": "20111222333",
        "address": "Av. Siempre Viva 742",
        "postal_code": "1406",
        "phone": "1133344455",
        "activo": "1",
    }
    result = client.post("/admin/tenants/new", data=payload, follow_redirects=False)
    assert result.status_code in (302, 303)

    async def _fetch():
        from sqlalchemy import select
        from app.models.tenant import Tenant

        async with db_session() as session:
            result = await session.execute(select(Tenant).where(Tenant.whatsapp_number == "whatsapp:+444"))
            return result.scalar_one_or_none()

    tenant = asyncio.run(_fetch())
    assert tenant is not None
    assert tenant.fantasy_name == "Consultorio Delta"
    assert tenant.first_name == "Luis"
    assert tenant.last_name == "Martinez"
    assert tenant.cuil == "20111222333"
    assert tenant.address == "Av. Siempre Viva 742"
    assert tenant.postal_code == "1406"
    assert tenant.phone == "1133344455"


def test_admin_create_tenant_duplicate_whatsapp_returns_form_error(client, db_session):
    asyncio.run(create_tenant(db_session, "Tenant Existente", "+5491150648909"))
    asyncio.run(
        create_user(
            db_session,
            "admin-dup-whatsapp@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )

    login(client, "admin-dup-whatsapp@test.com", "secret-123")
    response = client.get("/admin/tenants/new")
    csrf_token = response.text.split('name="csrf_token" value="')[1].split('"')[0]

    result = client.post(
        "/admin/tenants/new",
        data={
            "csrf_token": csrf_token,
            "nombre": "Tenant Duplicado",
            "whatsapp_number": "+5491150648909",
            "activo": "1",
        },
    )

    assert result.status_code == 200
    assert "Ese WhatsApp ya esta registrado." in result.text


def test_admin_create_tenant_duplicate_soft_deleted_whatsapp_returns_form_error(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Eliminado", "+5491150648910"))

    async def _soft_delete():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.deleted_at = datetime.now(timezone.utc)

    asyncio.run(_soft_delete())
    asyncio.run(
        create_user(
            db_session,
            "admin-dup-deleted@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )

    login(client, "admin-dup-deleted@test.com", "secret-123")
    response = client.get("/admin/tenants/new")
    csrf_token = response.text.split('name="csrf_token" value="')[1].split('"')[0]

    result = client.post(
        "/admin/tenants/new",
        data={
            "csrf_token": csrf_token,
            "nombre": "Tenant Duplicado Eliminado",
            "whatsapp_number": "+5491150648910",
            "activo": "1",
        },
    )

    assert result.status_code == 200
    assert "Ese WhatsApp ya esta registrado." in result.text
