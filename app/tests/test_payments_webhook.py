from __future__ import annotations

import asyncio

from app.models.payment import PaymentStatus
from app.models.turno import EstadoTurno
from app.tests.conftest import create_consultorio, create_paciente, create_payment, create_tenant, create_turno


def test_payment_webhook_approves_turno(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant P", "whatsapp:+900"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Sede P"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+9"))
    turno_id = asyncio.run(create_turno(db_session, paciente_id, consultorio_id))
    payment_id = asyncio.run(create_payment(db_session, tenant_id, paciente_id, turno_id))

    payload = {"type": "payment", "data": {"id": "mp-1", "status": "approved"}}
    response = client.post(f"/webhook/payments/mercadopago?payment_id={payment_id}", json=payload)
    assert response.status_code == 200

    async def _fetch():
        from app.models.payment import Payment
        from app.models.turno import Turno

        async with db_session() as session:
            payment = await session.get(Payment, payment_id)
            turno = await session.get(Turno, turno_id)
            return payment, turno

    payment, turno = asyncio.run(_fetch())
    assert payment.status == PaymentStatus.APPROVED
    assert payment.external_payment_id == "mp-1"
    assert turno.estado == EstadoTurno.CONFIRMADO


def test_payment_webhook_requires_signature_when_secret_configured(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant MP Secret", "whatsapp:+901"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Sede MP"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+9011"))
    turno_id = asyncio.run(create_turno(db_session, paciente_id, consultorio_id))
    payment_id = asyncio.run(create_payment(db_session, tenant_id, paciente_id, turno_id))

    async def _configure():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.payment_settings = {"mp_webhook_secret": "secret"}

    asyncio.run(_configure())

    payload = {"type": "payment", "data": {"id": "mp-2", "status": "approved"}}
    response = client.post(f"/webhook/payments/mercadopago?payment_id={payment_id}", json=payload)

    assert response.status_code == 401


def test_payment_webhook_accepts_valid_signature_and_stores_external_id(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant MP Signed", "whatsapp:+902"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Sede MP Signed"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+9021"))
    turno_id = asyncio.run(create_turno(db_session, paciente_id, consultorio_id))
    payment_id = asyncio.run(create_payment(db_session, tenant_id, paciente_id, turno_id))

    async def _configure():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.payment_settings = {"mp_webhook_secret": "secret"}

    asyncio.run(_configure())
    monkeypatch.setattr(
        "app.services.payment_service.MercadoPagoService.verify_webhook_signature",
        lambda raw_body, signature, secret: True,
    )

    payload = {"type": "payment", "data": {"id": "mp-3", "status": "approved"}}
    response = client.post(
        f"/webhook/payments/mercadopago?payment_id={payment_id}",
        json=payload,
        headers={"x-signature": "ts=1,v1=test"},
    )

    assert response.status_code == 200

    async def _fetch_payment():
        from app.models.payment import Payment

        async with db_session() as session:
            return await session.get(Payment, payment_id)

    payment = asyncio.run(_fetch_payment())
    assert payment.status == PaymentStatus.APPROVED
    assert payment.external_payment_id == "mp-3"
