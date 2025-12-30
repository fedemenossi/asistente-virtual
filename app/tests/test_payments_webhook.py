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
    assert turno.estado == EstadoTurno.CONFIRMADO
