from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.core.security import hash_password
from app.models.user import UserRole
from app.tests.conftest import create_paciente, create_tenant, create_user, login


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_manual_invoice_preview_is_transient_and_uses_patient_fiscal_profile(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Manual Tenant", "whatsapp:+549110000501"))
    patient_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+549110000502", iva_condition="consumidor_final"))
    async def seed():
        from app.models.arca_billable_item import ArcaBillableItem
        async with db_session() as session:
            async with session.begin():
                item = ArcaBillableItem(tenant_id=tenant_id, code="SERV", name="Consulta", unit_price=1000, currency="PES", concepto=2, active=True)
                session.add(item); await session.flush(); return item.id
    item_id = asyncio.run(seed())
    asyncio.run(create_user(db_session, "manual@test.com", hash_password("secret"), UserRole.TENANT_ADMIN.value, tenant_id))
    login(client, "manual@test.com", "secret")
    form = client.get("/t/billing/manual/new")
    preview = client.post("/t/billing/manual/preview", data={"csrf_token": _csrf(form.text), "patient_id": patient_id, "receiver_name": "Juan Perez", "receiver_document_type": "DNI", "receiver_document_number": "12345678", "receiver_iva_condition": "consumidor_final", "receiver_email": "juan@example.com", "item_id": "custom", "selected_item_id": item_id, "amount": "1250.50", "service_start": "2026-07-17", "service_end": "2026-07-18", "sale_condition": "Transferencia", "send_email": "on"})
    assert preview.status_code == 200
    assert "Previsualizacion temporal" in preview.text
    assert "1250.50" in preview.text
    # The preview must not persist a fiscal record before explicit confirmation.
    async def invoices():
        from app.models.arca_invoice import ArcaInvoice
        async with db_session() as session: return len((await session.execute(select(ArcaInvoice).where(ArcaInvoice.tenant_id == tenant_id))).scalars().all())
    assert asyncio.run(invoices()) == 0


def test_manual_invoice_preview_uses_selected_catalog_value_and_keeps_diagnosis(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Manual Catalog Value", "whatsapp:+549110000511"))
    patient_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+549110000512", iva_condition="consumidor_final"))

    async def seed():
        from app.models.arca_billable_item import ArcaBillableItem

        async with db_session() as session:
            async with session.begin():
                consultation = ArcaBillableItem(tenant_id=tenant_id, code="CONS", name="Consulta", unit_price=1000, currency="PES", concepto=2, active=True)
                procedure = ArcaBillableItem(tenant_id=tenant_id, code="PROC", name="Procedimiento", unit_price=2500, currency="PES", concepto=2, active=True)
                session.add_all([consultation, procedure])
                await session.flush()
                return consultation.id, procedure.id

    consultation_id, procedure_id = asyncio.run(seed())
    asyncio.run(create_user(db_session, "manual-catalog@test.com", hash_password("secret"), UserRole.TENANT_ADMIN.value, tenant_id))
    login(client, "manual-catalog@test.com", "secret")

    form = client.get("/t/billing/manual/new")
    assert "Importe personalizado" in form.text
    assert 'name="diagnosis"' in form.text
    preview = client.post(
        "/t/billing/manual/preview",
        data={
            "csrf_token": _csrf(form.text),
            "patient_id": patient_id,
            "receiver_name": "Juan Perez",
            "receiver_document_type": "DNI",
            "receiver_document_number": "12345678",
            "receiver_iva_condition": "consumidor_final",
            "receiver_email": "juan@example.com",
            "item_id": consultation_id,
            "amount": "9999.99",
            "diagnosis": "Control postoperatorio",
            "service_start": "2026-07-17",
            "service_end": "2026-07-18",
            "sale_condition": "Transferencia",
        },
    )

    assert preview.status_code == 200
    assert "1000.00" in preview.text
    assert "Control postoperatorio" in preview.text
