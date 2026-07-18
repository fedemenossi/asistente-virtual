from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.tests.conftest import create_tenant, create_user, login


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token no encontrado"
    return match.group(1)


def _contact_data(csrf_token: str, **overrides) -> dict[str, str]:
    data = {
        "csrf_token": csrf_token,
        "contact_type": "person",
        "name": "Ana Garcia",
        "document_type": "DNI",
        "document_number": "27654321",
        "iva_condition": "consumidor_final",
        "email": "ana@example.com",
        "active": "1",
    }
    data.update(overrides)
    return data


def test_tenant_admin_manages_fiscal_contacts_and_audits(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Contacts", "whatsapp:+549110000301"))
    asyncio.run(
        create_user(
            db_session,
            "contacts@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "contacts@example.com", "secret-123")

    page = client.get("/t/billing/fiscal-contacts")
    assert page.status_code == 200
    assert "Contactos fiscales" in page.text

    new_page = client.get("/t/billing/fiscal-contacts/new")
    response = client.post(
        "/t/billing/fiscal-contacts/new",
        data=_contact_data(_csrf(new_page.text)),
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    listing = client.get("/t/billing/fiscal-contacts?q=Ana")
    assert "Ana Garcia" in listing.text
    assert "DNI 27654321" in listing.text

    async def _contact_and_logs():
        from app.models.billing_fiscal_contact import BillingFiscalContact

        async with db_session() as session:
            contact = await session.scalar(
                select(BillingFiscalContact).where(BillingFiscalContact.tenant_id == tenant_id)
            )
            logs = list(
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == tenant_id,
                            AuditLog.entity == "billing_fiscal_contact",
                        )
                    )
                ).scalars()
            )
            return contact, logs

    contact, logs = asyncio.run(_contact_and_logs())
    assert contact is not None
    assert contact.document_number == "27654321"
    assert contact.active is True
    assert [log.action for log in logs] == ["create"]

    edit_page = client.get(f"/t/billing/fiscal-contacts/{contact.id}/edit")
    response = client.post(
        f"/t/billing/fiscal-contacts/{contact.id}/edit",
        data=_contact_data(
            _csrf(edit_page.text),
            contact_type="organization",
            name="Clinica del Sur SA",
            document_type="CUIT",
            document_number="30-71234567-9",
            iva_condition="responsable_inscripto",
            email="administracion@clinica.example.com",
        ),
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _updated_contact():
        from app.models.billing_fiscal_contact import BillingFiscalContact

        async with db_session() as session:
            return await session.get(BillingFiscalContact, contact.id)

    updated = asyncio.run(_updated_contact())
    assert updated.contact_type == "organization"
    assert updated.document_type == "CUIT"
    assert updated.document_number == "30712345679"
    assert updated.iva_condition == "responsable_inscripto"

    edit_page = client.get(f"/t/billing/fiscal-contacts/{contact.id}/edit")
    response = client.post(
        f"/t/billing/fiscal-contacts/{contact.id}/deactivate",
        data={"csrf_token": _csrf(edit_page.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    inactive_listing = client.get("/t/billing/fiscal-contacts")
    assert "Inactivo" in inactive_listing.text

    async def _audit_actions():
        async with db_session() as session:
            result = await session.execute(
                select(AuditLog.action).where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.entity == "billing_fiscal_contact",
                    AuditLog.entity_id == contact.id,
                )
            )
            return set(result.scalars().all())

    assert asyncio.run(_audit_actions()) == {"create", "update", "deactivate"}


def test_fiscal_contact_identity_is_unique_per_tenant_and_isolated(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant A", "whatsapp:+549110000302"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant B", "whatsapp:+549110000303"))
    asyncio.run(
        create_user(
            db_session,
            "contacts-a@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "contacts-b@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_b,
        )
    )
    login(client, "contacts-a@example.com", "secret-123")
    page = client.get("/t/billing/fiscal-contacts/new")
    client.post(
        "/t/billing/fiscal-contacts/new",
        data=_contact_data(_csrf(page.text), document_number="20123456"),
        follow_redirects=False,
    )
    duplicate_page = client.get("/t/billing/fiscal-contacts/new")
    duplicate = client.post(
        "/t/billing/fiscal-contacts/new",
        data=_contact_data(_csrf(duplicate_page.text), document_number="20.123.456"),
    )
    assert duplicate.status_code == 200
    assert "Ya existe un contacto fiscal" in duplicate.text

    client.get("/logout")
    login(client, "contacts-b@example.com", "secret-123")
    page = client.get("/t/billing/fiscal-contacts/new")
    same_identity = client.post(
        "/t/billing/fiscal-contacts/new",
        data=_contact_data(_csrf(page.text), document_number="20123456"),
        follow_redirects=False,
    )
    assert same_identity.status_code in (302, 303)

    async def _tenant_a_contact_id():
        from app.models.billing_fiscal_contact import BillingFiscalContact

        async with db_session() as session:
            return await session.scalar(
                select(BillingFiscalContact.id).where(BillingFiscalContact.tenant_id == tenant_a)
            )

    tenant_a_contact_id = asyncio.run(_tenant_a_contact_id())
    assert client.get(f"/t/billing/fiscal-contacts/{tenant_a_contact_id}/edit").status_code == 404


def test_fiscal_contacts_respect_billing_feature_gate(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Gate", "whatsapp:+549110000304"))
    asyncio.run(
        create_user(
            db_session,
            "contacts-gate@example.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(_set_billing_feature(db_session, tenant_id, enabled=False))
    login(client, "contacts-gate@example.com", "secret-123")

    assert client.get("/t/billing/fiscal-contacts").status_code == 403


async def _set_billing_feature(db_session, tenant_id: int, *, enabled: bool) -> None:
    from app.models.tenant_feature import TenantFeature

    async with db_session() as session:
        async with session.begin():
            feature = TenantFeature(tenant_id=tenant_id, feature_key="billing_arca", enabled=enabled)
            session.add(feature)
