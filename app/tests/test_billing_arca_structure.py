from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.integrations.arca.wsaa_client import AccessTicket
from app.integrations.arca.wsfe_client import WsfeError, WsfeResult
from app.models.arca_billable_item import ArcaBillableItem
from app.models.arca_invoice import ArcaInvoice, ArcaInvoiceStatus
from app.models.arca_invoice_event import ArcaInvoiceEvent
from app.models.billing_diagnostic import BillingDiagnostic
from app.models.billing_email_log import BillingEmailLog
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.billing_invoice_line import BillingInvoiceLine
from app.models.paciente import Paciente
from app.integrations.consultorio_movil import ConsultorioMovilAccessBlocked
from app.services.billing_consultorio_sync_job_service import (
    BillingConsultorioSyncJob,
    CONSULTORIO_MOVIL_BLOCKED_MESSAGE,
    _jobs,
    _latest_imported_attended_at,
    _run_billing_consultorio_sync_job,
)
from app.models.tenant import Tenant
from app.models.tenant_feature import TenantFeature
from app.models.user import UserRole
from app.services.arca_service import (
    ArcaConnectionTestResult,
    ArcaConnectivityError,
    ArcaEmissionError,
    ArcaInvoiceAlreadyExists,
    ArcaService,
)
from app.services.billing_arca_settings_service import decrypt_secret, encrypt_secret
from app.services.billing_invoice_document_service import (
    BillingInvoiceDocumentService,
    BillingInvoiceEmailService,
)
from app.services.messaging_service import MessagingService
from app.services.tenant_feature_service import TenantFeatureService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_user, login


async def _create_invoice(db_session, tenant_id: int, *, cbte_nro: int, amount: Decimal) -> int:
    async with db_session() as session:
        async with session.begin():
            invoice = ArcaInvoice(
                tenant_id=tenant_id,
                represented_cuit="20123456789",
                environment="homo",
                pto_vta=1,
                cbte_tipo=11,
                cbte_nro=cbte_nro,
                concepto=2,
                doc_tipo=99,
                doc_nro="0",
                imp_total=amount,
                imp_tot_conc=Decimal("0.00"),
                imp_neto=amount,
                imp_op_ex=Decimal("0.00"),
                imp_trib=Decimal("0.00"),
                imp_iva=Decimal("0.00"),
                mon_id="PES",
                mon_cotiz=Decimal("1.000000"),
                status=ArcaInvoiceStatus.DRAFT,
            )
            session.add(invoice)
            await session.flush()
            return invoice.id


async def _create_arca_emission_seed(
    db_session,
    tenant_id: int,
    *,
    diagnosis: str | None = "Bronquitis aguda",
    arca_invoice_id: int | None = None,
    patient_email: str | None = None,
) -> tuple[int, int]:
    async with db_session() as session:
        async with session.begin():
            tenant = await session.get(Tenant, tenant_id)
            tenant.arca_settings = {
                "enabled": True,
                "environment": "homo",
                "represented_cuit": "20123456789",
                "service": "wsfe",
                "default_pto_vta": 3,
                "default_cbte_tipo": 11,
                "default_concepto": 2,
                "default_currency": "PES",
                "default_mon_cotiz": "1",
                "certificate_encrypted": encrypt_secret("CERT"),
                "private_key_encrypted": encrypt_secret("KEY"),
                "has_certificate": True,
                "has_private_key": True,
            }
            item = ArcaBillableItem(
                tenant_id=tenant_id,
                code="CONSULTA",
                name="Consulta medica",
                unit_price=Decimal("1500.00"),
                currency="PES",
                concepto=2,
                active=True,
            )
            consultation = BillingExternalConsultation(
                tenant_id=tenant_id,
                consultorio_id=None,
                arca_invoice_id=arca_invoice_id,
                external_provider="consultorio_movil",
                external_id=f"att-{datetime.now(timezone.utc).timestamp()}",
                attended_at=datetime(2026, 7, 4, 10, 0),
                patient_name="Juan Perez",
                patient_document="30111222",
                patient_email=patient_email,
                insurance_name="OSDE",
                practice_name="Consulta",
                diagnosis_original=diagnosis,
                diagnosis=diagnosis,
                raw_payload_json={"patient": {"email": patient_email}} if patient_email else None,
            )
            session.add_all([item, consultation])
            await session.flush()
            return item.id, consultation.id


class _FakeWsaaForEmission:
    def __init__(self, settings):
        self.settings = settings

    def request_ticket(self):
        return AccessTicket(
            token="token",
            sign="sign",
            expiration_time=datetime.now(timezone.utc) + timedelta(hours=1),
        )


async def _emit_authorized_test_invoice(db_session, tenant_id: int, item_id: int, consultation_id: int) -> int:
    class FakeWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            return WsfeResult(data={"CbteNro": 50})

        def solicitar_cae(self, request):
            return WsfeResult(
                data={
                    "FeCabResp": {"Resultado": "A"},
                    "FeDetResp": {
                        "FEDetResponse": {
                            "Resultado": "A",
                            "CAE": "12345678901234",
                            "CAEFchVto": "20260714",
                        }
                    },
                }
            )

        def consultar_comprobante(self, pto_vta, cbte_tipo, cbte_nro):
            raise AssertionError("No debe recuperar")

    async with db_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        item = await session.get(ArcaBillableItem, item_id)
        consultation = await session.get(BillingExternalConsultation, consultation_id)
        service = ArcaService(
            session,
            wsaa_factory=_FakeWsaaForEmission,
            wsfe_factory=FakeWsfe,
        )
        result = await service.emit_invoice_for_consultation(tenant, consultation, item)
        await session.commit()
        return result.invoice.id


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_billing_arca_route_requires_tenant_login(client):
    response = client.get("/t/billing-arca", follow_redirects=False)
    assert response.status_code in (302, 303)


def test_tenant_can_access_billing_arca_placeholder_and_sidebar(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA", "whatsapp:+610"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-arca@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-arca@test.com", "secret-123")

    response = client.get("/t/billing-arca")
    assert response.status_code == 200
    assert "Facturacion ARCA" in response.text
    assert 'href="/t/billing-arca"' in response.text
    assert 'href="/t/settings/billing-arca"' in response.text

    settings_response = client.get("/t/settings/billing-arca")
    assert settings_response.status_code == 200
    assert "Configuracion ARCA" in settings_response.text


def test_billing_arca_settings_save_encrypts_certificate_and_key(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Settings", "whatsapp:+615"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-arca-settings@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-arca-settings@test.com", "secret-123")

    page = client.get("/t/settings/billing-arca")
    csrf = _csrf(page.text)
    certificate = "-----BEGIN CERTIFICATE-----\nCERTDATA\n-----END CERTIFICATE-----"
    private_key = "-----BEGIN PRIVATE KEY-----\nKEYDATA\n-----END PRIVATE KEY-----"
    response = client.post(
        "/t/settings/billing-arca",
        data={
            "csrf_token": csrf,
            "enabled": "on",
            "represented_cuit": "20123456789",
            "environment": "homo",
            "default_pto_vta": "3",
            "default_cbte_tipo": "11",
            "default_concepto": "2",
            "default_currency": "PES",
            "default_mon_cotiz": "1",
            "fiscal_name": "Consultorio Demo",
            "fiscal_address": "Calle Fiscal 123",
            "gross_income": "12345",
            "tax_condition": "Monotributo",
            "activity_code": "862110",
            "professional_legend": "Medica especialista en Ginecologia y Obstetricia M.N. 122.674",
            "certificate_pem": certificate,
            "private_key_pem": private_key,
            "key_passphrase": "pass-secret",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _settings():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            return tenant.arca_settings

    settings = asyncio.run(_settings())
    assert settings["represented_cuit"] == "20123456789"
    assert settings["default_pto_vta"] == 3
    assert settings["has_certificate"] is True
    assert settings["has_private_key"] is True
    assert settings["professional_legend"] == "Medica especialista en Ginecologia y Obstetricia M.N. 122.674"
    assert certificate not in settings["certificate_encrypted"]
    assert private_key not in settings["private_key_encrypted"]
    assert decrypt_secret(settings["certificate_encrypted"]) == certificate
    assert decrypt_secret(settings["private_key_encrypted"]) == private_key
    assert decrypt_secret(settings["key_passphrase_encrypted"]) == "pass-secret"


def test_billing_arca_settings_validation_errors(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Invalid", "whatsapp:+616"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-arca-invalid@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-arca-invalid@test.com", "secret-123")
    page = client.get("/t/settings/billing-arca")
    response = client.post(
        "/t/settings/billing-arca",
        data={
            "csrf_token": _csrf(page.text),
            "represented_cuit": "20",
            "environment": "bad",
            "default_pto_vta": "0",
            "default_cbte_tipo": "1",
            "default_concepto": "9",
            "default_currency": "BAD",
            "default_mon_cotiz": "0",
            "fiscal_name": "",
            "fiscal_address": "",
        },
    )
    assert response.status_code == 200
    assert "El CUIT debe tener 11 digitos" in response.text
    assert "Ambiente invalido" in response.text


def test_billing_arca_test_button_is_prepared_without_external_call(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Test", "whatsapp:+617"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-arca-test@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-arca-test@test.com", "secret-123")
    page = client.get("/t/settings/billing-arca")
    response = client.post(
        "/t/settings/billing-arca/test",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/t/settings/billing-arca"


def test_billing_arca_test_button_uses_service(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Test OK", "whatsapp:+623"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-arca-test-ok@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-arca-test-ok@test.com", "secret-123")
    captured = {}

    class FakeArcaService:
        def __init__(self, session):
            captured["created"] = True

        async def test_connection(self, tenant):
            captured["tenant_id"] = tenant.id
            return ArcaConnectionTestResult(
                environment="homo",
                represented_cuit="20123456789",
                dummy={"AppServer": "OK", "DbServer": "OK", "AuthServer": "OK"},
                points_of_sale=[{"Nro": 1}],
                used_cached_ticket=False,
            )

    monkeypatch.setattr("app.web.tenant.views.ArcaService", FakeArcaService)
    page = client.get("/t/settings/billing-arca")
    response = client.post(
        "/t/settings/billing-arca/test",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert captured["created"] is True
    assert captured["tenant_id"] == tenant_id


def test_arca_service_gets_ticket_and_reuses_cache(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Service", "whatsapp:+624"))

    async def _configure():
        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.arca_settings = {
                    "enabled": True,
                    "environment": "homo",
                    "represented_cuit": "20123456789",
                    "service": "wsfe",
                    "certificate_encrypted": encrypt_secret("CERT"),
                    "private_key_encrypted": encrypt_secret("KEY"),
                    "has_certificate": True,
                    "has_private_key": True,
                }

    asyncio.run(_configure())
    calls = {"wsaa": 0, "auth": []}

    class FakeWsaa:
        def __init__(self, settings):
            self.settings = settings

        def request_ticket(self):
            calls["wsaa"] += 1
            return AccessTicket(
                token=f"token-{calls['wsaa']}",
                sign=f"sign-{calls['wsaa']}",
                expiration_time=datetime.now(timezone.utc) + timedelta(hours=1),
            )

    class FakeWsfe:
        def __init__(self, settings, auth_provider):
            self.auth_provider = auth_provider

        def dummy(self):
            return WsfeResult(data={"AppServer": "OK", "DbServer": "OK", "AuthServer": "OK"})

        def get_puntos_venta(self):
            calls["auth"].append(self.auth_provider())
            return WsfeResult(data={"PtoVenta": [{"Nro": 1, "EmisionTipo": "CAE"}]})

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            service = ArcaService(session, wsaa_factory=FakeWsaa, wsfe_factory=FakeWsfe)
            first = await service.test_connection(tenant)
            second = await service.test_connection(tenant)
            await session.commit()
            return first, second

    first, second = asyncio.run(_run())
    assert calls["wsaa"] == 1
    assert calls["auth"][0]["Token"] == "token-1"
    assert first.used_cached_ticket is False
    assert second.used_cached_ticket is True
    assert second.points_of_sale == [{"Nro": 1, "EmisionTipo": "CAE"}]


def test_arca_service_wraps_wsfe_errors(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Error", "whatsapp:+625"))

    async def _configure():
        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.arca_settings = {
                    "enabled": True,
                    "environment": "homo",
                    "represented_cuit": "20123456789",
                    "service": "wsfe",
                    "certificate_encrypted": encrypt_secret("CERT"),
                    "private_key_encrypted": encrypt_secret("KEY"),
                    "has_certificate": True,
                    "has_private_key": True,
                }

    asyncio.run(_configure())

    class FakeWsaa:
        def __init__(self, settings):
            pass

        def request_ticket(self):
            return AccessTicket(
                token="token",
                sign="sign",
                expiration_time=datetime.now(timezone.utc) + timedelta(hours=1),
            )

    class FailingWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def dummy(self):
            raise WsfeError("WSFE caido")

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            service = ArcaService(session, wsaa_factory=FakeWsaa, wsfe_factory=FailingWsfe)
            try:
                await service.test_connection(tenant)
            except ArcaConnectivityError as exc:
                return str(exc)
            return ""

    message = asyncio.run(_run())
    assert "WSFE caido" in message


def test_billing_arca_feature_disabled_blocks_route_and_hides_menu(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Off", "whatsapp:+611"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-arca-off@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _disable():
        async with db_session() as session:
            async with session.begin():
                service = TenantFeatureService(session)
                await service.sync_tenant_with_registry(tenant_id)
                await service.set_flags(tenant_id, {"billing_arca": False}, updated_by=None)

    asyncio.run(_disable())
    login(client, "tenant-arca-off@test.com", "secret-123")

    blocked = client.get("/t/billing-arca")
    assert blocked.status_code == 403

    dashboard = client.get("/t/dashboard")
    assert dashboard.status_code == 200
    assert 'href="/t/billing-arca"' not in dashboard.text
    assert 'href="/t/settings/billing-arca"' not in dashboard.text


def test_billing_arca_list_is_scoped_to_current_tenant(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant ARCA A", "whatsapp:+612"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant ARCA B", "whatsapp:+613"))
    invoice_a = asyncio.run(
        _create_invoice(db_session, tenant_a, cbte_nro=101, amount=Decimal("1500.00"))
    )
    invoice_b = asyncio.run(
        _create_invoice(db_session, tenant_b, cbte_nro=202, amount=Decimal("2500.00"))
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-arca-a@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    login(client, "tenant-arca-a@test.com", "secret-123")

    response = client.get("/t/billing-arca")
    assert response.status_code == 200
    assert "00001-00000101" in response.text
    assert "00001-00000202" not in response.text
    assert "Fecha facturacion" in response.text

    own_detail = client.get(f"/t/billing-arca/{invoice_a}")
    assert own_detail.status_code == 200
    assert "Comprobante ARCA" in own_detail.text

    other_detail = client.get(f"/t/billing-arca/{invoice_b}")
    assert other_detail.status_code == 404


def test_billing_arca_billable_items_crud_and_tenant_scope(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Items A", "whatsapp:+618"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Items B", "whatsapp:+619"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-items-a@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    login(client, "tenant-items-a@test.com", "secret-123")

    page = client.get("/t/billing-arca/items/new")
    assert page.status_code == 200
    create_response = client.post(
        "/t/billing-arca/items/new",
        data={
            "csrf_token": _csrf(page.text),
            "code": "CONSULTA",
            "name": "Consulta medica",
            "description": "Consulta general",
            "unit_price": "1500,50",
            "currency": "PES",
            "concepto": "2",
            "active": "on",
        },
        follow_redirects=False,
    )
    assert create_response.status_code in (302, 303)

    async def _fetch_item():
        async with db_session() as session:
            item = await session.scalar(
                select(ArcaBillableItem).where(
                    ArcaBillableItem.tenant_id == tenant_a,
                    ArcaBillableItem.code == "CONSULTA",
                )
            )
            other = ArcaBillableItem(
                tenant_id=tenant_b,
                code="OTRO",
                name="Otro tenant",
                unit_price=Decimal("999.00"),
                currency="PES",
                concepto=2,
                active=True,
            )
            session.add(other)
            await session.commit()
            return item.id

    item_id = asyncio.run(_fetch_item())
    listing = client.get("/t/billing-arca/items")
    assert "CONSULTA" in listing.text
    assert "OTRO" not in listing.text

    duplicate = client.post(
        "/t/billing-arca/items/new",
        data={
            "csrf_token": _csrf(client.get("/t/billing-arca/items/new").text),
            "code": "CONSULTA",
            "name": "Duplicado",
            "unit_price": "1",
            "currency": "PES",
            "concepto": "2",
            "active": "on",
        },
    )
    assert duplicate.status_code == 200
    assert "Ya existe un item con ese codigo" in duplicate.text

    edit_page = client.get(f"/t/billing-arca/items/{item_id}/edit")
    edit_response = client.post(
        f"/t/billing-arca/items/{item_id}/edit",
        data={
            "csrf_token": _csrf(edit_page.text),
            "code": "CONSULTA-2",
            "name": "Consulta actualizada",
            "description": "",
            "unit_price": "1800",
            "currency": "PES",
            "concepto": "2",
            "active": "on",
        },
        follow_redirects=False,
    )
    assert edit_response.status_code in (302, 303)

    settings_edit_page = client.get(f"/t/settings/billing/items/{item_id}/edit")
    settings_edit_response = client.post(
        f"/t/settings/billing/items/{item_id}/edit",
        data={
            "csrf_token": _csrf(settings_edit_page.text),
            "code": "CONSULTA-3",
            "name": "Consulta settings",
            "description": "",
            "unit_price": "1900",
            "currency": "PES",
            "concepto": "2",
            "active": "on",
        },
        follow_redirects=False,
    )
    assert settings_edit_response.status_code in (302, 303)

    delete_page = client.get(f"/t/billing-arca/items/{item_id}/edit")
    delete_response = client.post(
        f"/t/billing-arca/items/{item_id}/delete",
        data={"csrf_token": _csrf(delete_page.text)},
        follow_redirects=False,
    )
    assert delete_response.status_code in (302, 303)

    async def _active():
        async with db_session() as session:
            item = await session.get(ArcaBillableItem, item_id)
            return item.code, item.name, item.active

    code, name, active = asyncio.run(_active())
    assert code == "CONSULTA-3"
    assert name == "Consulta settings"
    assert active is False


def test_billing_diagnostics_crud_and_tenant_scope(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Diagnostics A", "whatsapp:+6181"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Diagnostics B", "whatsapp:+6182"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-diagnostics-a@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    login(client, "tenant-diagnostics-a@test.com", "secret-123")

    page = client.get("/t/settings/billing/diagnostics/new")
    assert page.status_code == 200
    create_response = client.post(
        "/t/settings/billing/diagnostics/new",
        data={
            "csrf_token": _csrf(page.text),
            "code": "CONTROL",
            "name": "Control ginecologico",
            "description": "Control anual",
            "active": "on",
            "default_diagnostic": "on",
        },
        follow_redirects=False,
    )
    assert create_response.status_code in (302, 303)

    async def _fetch_diagnostic():
        async with db_session() as session:
            diagnostic = await session.scalar(
                select(BillingDiagnostic).where(
                    BillingDiagnostic.tenant_id == tenant_a,
                    BillingDiagnostic.code == "CONTROL",
                )
            )
            other = BillingDiagnostic(
                tenant_id=tenant_b,
                code="OTRO",
                name="Otro tenant",
                active=True,
            )
            session.add(other)
            await session.commit()
            return diagnostic.id

    diagnostic_id = asyncio.run(_fetch_diagnostic())
    listing = client.get("/t/settings/billing/diagnostics")
    assert "CONTROL" in listing.text
    assert "Control ginecologico" in listing.text
    assert "OTRO" not in listing.text

    duplicate = client.post(
        "/t/settings/billing/diagnostics/new",
        data={
            "csrf_token": _csrf(client.get("/t/settings/billing/diagnostics/new").text),
            "code": "CONTROL",
            "name": "Duplicado",
            "active": "on",
        },
    )
    assert duplicate.status_code == 200
    assert "Ya existe un diagnostico con ese codigo" in duplicate.text

    edit_page = client.get(f"/t/settings/billing/diagnostics/{diagnostic_id}/edit")
    edit_response = client.post(
        f"/t/settings/billing/diagnostics/{diagnostic_id}/edit",
        data={
            "csrf_token": _csrf(edit_page.text),
            "code": "CONTROL-2",
            "name": "Control actualizado",
            "description": "",
            "active": "on",
        },
        follow_redirects=False,
    )
    assert edit_response.status_code in (302, 303)

    delete_page = client.get(f"/t/settings/billing/diagnostics/{diagnostic_id}/edit")
    delete_response = client.post(
        f"/t/settings/billing/diagnostics/{diagnostic_id}/delete",
        data={"csrf_token": _csrf(delete_page.text)},
        follow_redirects=False,
    )
    assert delete_response.status_code in (302, 303)

    async def _active():
        async with db_session() as session:
            diagnostic = await session.get(BillingDiagnostic, diagnostic_id)
            return diagnostic.code, diagnostic.name, diagnostic.active

    code, name, active = asyncio.run(_active())
    assert code == "CONTROL-2"
    assert name == "Control actualizado"
    assert active is False


def test_billing_arca_feature_registered_for_sync(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant ARCA Sync", "whatsapp:+614"))

    async def _sync():
        async with db_session() as session:
            async with session.begin():
                await TenantFeatureService(session).sync_all_tenants_with_registry()
                result = await session.execute(
                    select(TenantFeature).where(
                        TenantFeature.tenant_id == tenant_id,
                        TenantFeature.feature_key == "billing_arca",
                    )
                )
                return result.scalar_one_or_none()

    feature = asyncio.run(_sync())
    assert feature is not None
    assert feature.enabled is True


def test_billing_pending_imports_attended_consultations_and_shows_invoiced_locked(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Pending", "whatsapp:+620"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Sede Cabildo",
            proveedor_turnos="consultorio_movil",
            configuracion_externa={
                "cabildo": {
                    "user": "cm-user",
                    "password": "cm-pass",
                    "staff_id": "77",
                }
            },
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-pending@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "1144445555",
            nombre="Juan",
            apellido="Perez",
            dni="30111222",
            tipo_documento="DNI",
            numero_documento="30111222",
            document_number_normalized="30111222",
            email="juan@example.com",
            obra_social="OSDE",
        )
    )
    invoice_id = asyncio.run(
        _create_invoice(db_session, tenant_id, cbte_nro=301, amount=Decimal("100.00"))
    )

    async def _seed_invoiced():
        async with db_session() as session:
            async with session.begin():
                row = BillingExternalConsultation(
                    tenant_id=tenant_id,
                    consultorio_id=consultorio_id,
                    arca_invoice_id=invoice_id,
                    external_provider="consultorio_movil",
                    external_id="att-2",
                    patient_name="Paciente Facturado",
                )
                session.add(row)

    asyncio.run(_seed_invoiced())
    login(client, "tenant-pending@test.com", "secret-123")

    page = client.get("/t/billing/pending?date_from=2026-07-01&date_to=2026-07-02")
    assert page.status_code == 200
    csv_content = (
        "id,fecha,paciente,email,obra social,profesional,practica,diagnostico\n"
        "att-1,01/07/2026 10:30,Juan Perez,,OSDE,Dra Gomez,Consulta,Control\n"
        "att-2,01/07/2026 11:30,Paciente Facturado,facturado@example.com,Swiss Medical,Dra Gomez,Consulta,Ya facturado\n"
    ).encode("utf-8-sig")
    response = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("atendidas.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _rows():
        async with db_session() as session:
            result = await session.execute(
                select(BillingExternalConsultation).where(
                    BillingExternalConsultation.tenant_id == tenant_id
                )
            )
            return list(result.scalars().all())

    rows = asyncio.run(_rows())
    assert len(rows) == 2
    imported = next(row for row in rows if row.external_id == "att-1")
    invoiced = next(row for row in rows if row.external_id == "att-2")
    assert imported.patient_name == "Juan Perez"
    assert imported.patient_document == "30111222"
    assert imported.patient_email == "juan@example.com"
    assert imported.insurance_name == "OSDE"
    assert imported.diagnosis == "Control"
    assert imported.diagnosis_original == "Control"
    assert imported.arca_invoice_id is None
    assert invoiced.arca_invoice_id == invoice_id

    listing = client.get(response.headers["location"])
    assert "Juan Perez" in listing.text
    assert "juan@example.com" in listing.text
    assert "OSDE" in listing.text
    assert "Paciente Facturado" not in listing.text

    finalized = client.get("/t/billing/finalized?status=billed")
    assert "Paciente Facturado" in finalized.text
    assert "Facturado" in finalized.text
    assert "$ 100.00" in finalized.text
    assert "Ya facturado" in finalized.text

    by_dni = client.get("/t/billing/pending?dni=30111222")
    assert "Juan Perez" in by_dni.text
    by_insurance = client.get("/t/billing/pending?obra_social=OSDE")
    assert "Juan Perez" in by_insurance.text


def test_billing_pending_import_supports_real_attended_csv_format(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Real CSV", "whatsapp:+636"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-real-csv@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Andrea",
            apellido="Blumtritt",
            dni="30123456",
            tipo_documento="DNI",
            numero_documento="30123456",
            document_number_normalized="30123456",
            email="andrea@example.com",
            obra_social="OSDE",
        )
    )
    login(client, "tenant-real-csv@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    csv_content = (
        '"Fecha","Paciente","Médico","Financiador"\n'
        '"03/07/2026","Andrea Blumtritt (61868706701)","Marìa Laura Langdon","OSDE"\n'
    ).encode("utf-8-sig")

    response = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("pacientes_atendidos.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/t/billing/pending"

    listing = client.get(response.headers["location"])
    assert "Andrea Blumtritt" in listing.text
    assert "DNI 30123456" in listing.text
    assert "OSDE" in listing.text

    async def _fetch():
        async with db_session() as session:
            return await session.scalar(
                select(BillingExternalConsultation).where(
                    BillingExternalConsultation.tenant_id == tenant_id,
                    BillingExternalConsultation.patient_name == "Andrea Blumtritt",
                )
            )

    row = asyncio.run(_fetch())
    assert row.patient_external_id == "61868706701"
    assert row.professional_name == "Marìa Laura Langdon"

def test_billing_pending_import_accepts_date_with_seconds(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant CSV Seconds", "whatsapp:+639"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-csv-seconds@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Andrea",
            apellido="Blumtritt",
            dni="30123456",
            tipo_documento="DNI",
            numero_documento="30123456",
            document_number_normalized="30123456",
            email="andrea@example.com",
            obra_social="OSDE",
        )
    )
    login(client, "tenant-csv-seconds@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    csv_content = (
        '"Fecha","Paciente","Medico","Financiador"\n'
        '"03/07/2026 21:15:30","Andrea Blumtritt (61868706701)","Maria Laura Langdon","OSDE"\n'
    ).encode("utf-8-sig")

    response = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("pacientes_atendidos.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    listing = client.get(response.headers["location"])
    assert "03/07/2026" in listing.text

    async def _fetch():
        async with db_session() as session:
            return await session.scalar(
                select(BillingExternalConsultation).where(
                    BillingExternalConsultation.tenant_id == tenant_id,
                    BillingExternalConsultation.patient_name == "Andrea Blumtritt",
                )
            )

    row = asyncio.run(_fetch())
    assert row.attended_at == datetime(2026, 7, 4, 0, 15, 30)


def test_billing_pending_import_date_only_keeps_local_ba_day(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant CSV Date Only BA", "whatsapp:+6391"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-csv-date-only-ba@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Andrea",
            apellido="Blumtritt",
            dni="30123456",
            tipo_documento="DNI",
            numero_documento="30123456",
            document_number_normalized="30123456",
            email="andrea@example.com",
            obra_social="OSDE",
        )
    )
    login(client, "tenant-csv-date-only-ba@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    csv_content = (
        '"Fecha","Paciente","Medico","Financiador"\n'
        '"17/06/2026","Andrea Blumtritt (61868706701)","Maria Laura Langdon","OSDE"\n'
    ).encode("utf-8-sig")

    response = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("pacientes_atendidos.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    listing = client.get(response.headers["location"])
    assert "17/06/2026" in listing.text
    assert "16/06/2026" not in listing.text

    filtered = client.get("/t/billing/pending?date_from=2026-06-17&date_to=2026-06-17")
    assert "Andrea Blumtritt" in filtered.text

    async def _fetch():
        async with db_session() as session:
            return await session.scalar(
                select(BillingExternalConsultation).where(
                    BillingExternalConsultation.tenant_id == tenant_id,
                    BillingExternalConsultation.patient_name == "Andrea Blumtritt",
                )
            )

    row = asyncio.run(_fetch())
    assert row.attended_at == datetime(2026, 6, 17, 3, 0)


def test_billing_pending_import_accepts_cp1252_attended_csv(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant CSV CP1252", "whatsapp:+638"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-csv-cp1252@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Andrea",
            apellido="Blumtritt",
            dni="30123456",
            tipo_documento="DNI",
            numero_documento="30123456",
            document_number_normalized="30123456",
            email="andrea@example.com",
            obra_social="OSDE",
        )
    )
    login(client, "tenant-csv-cp1252@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    csv_content = (
        b'"Fecha","Paciente","M\xe9dico","Financiador"\n'
        b'"03/07/2026","Andrea Blumtritt (61868706701)","Maria Laura Langdon","OSDE"\n'
    )

    response = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("pacientes_atendidos.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/t/billing/pending"
    listing = client.get(response.headers["location"])
    assert "Andrea Blumtritt" in listing.text
    assert "DNI 30123456" in listing.text


def test_billing_csv_reimport_deduplicates_persisted_rows(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant CSV Reimport", "whatsapp:+637"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-csv-reimport@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Andrea",
            apellido="Blumtritt",
            dni="30123456",
            tipo_documento="DNI",
            numero_documento="30123456",
            document_number_normalized="30123456",
            email="andrea@example.com",
            obra_social="OSDE",
        )
    )
    login(client, "tenant-csv-reimport@test.com", "secret-123")
    csv_content = (
        '"Fecha","Paciente","Médico","Financiador"\n'
        '"03/07/2026","Andrea Blumtritt (61868706701)","Marìa Laura Langdon","OSDE"\n'
    ).encode("utf-8-sig")

    page = client.get("/t/billing/pending")
    first = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("pacientes_atendidos.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )
    assert first.status_code in (302, 303)
    first_listing = client.get(first.headers["location"])
    assert "Andrea Blumtritt" in first_listing.text

    second_page = client.get("/t/billing/pending")
    second = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(second_page.text)},
        files={"csv_file": ("pacientes_atendidos.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )
    assert second.status_code in (302, 303)
    assert second.headers["location"] == "/t/billing/pending"
    second_listing = client.get(second.headers["location"])
    assert "Andrea Blumtritt" in second_listing.text
    assert "DNI 30123456" in second_listing.text

    async def _count():
        async with db_session() as session:
            result = await session.execute(
                select(BillingExternalConsultation).where(
                    BillingExternalConsultation.tenant_id == tenant_id,
                    BillingExternalConsultation.patient_name == "Andrea Blumtritt",
                )
            )
            return len(list(result.scalars().all()))

    assert asyncio.run(_count()) == 1


def test_billing_csv_import_deduplicates_by_date_and_patient_name_across_files(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant CSV Natural Key", "whatsapp:+6371"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-csv-natural-key@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Andrea",
            apellido="Blumtritt",
            dni="30123456",
            tipo_documento="DNI",
            numero_documento="30123456",
            document_number_normalized="30123456",
            email="andrea@example.com",
            obra_social="OSDE",
        )
    )
    login(client, "tenant-csv-natural-key@test.com", "secret-123")
    csv_a = (
        '"Fecha","Paciente","Medico","Financiador"\n'
        '"17/06/2026","Andrea Blumtritt","Maria Laura Langdon","OSDE"\n'
    ).encode("utf-8-sig")
    csv_b = (
        '"Fecha","Paciente","Medico","Financiador"\n'
        '"17/06/2026","Andrea Blumtritt","Otra profesional","OSDE"\n'
    ).encode("utf-8-sig")

    page = client.get("/t/billing/pending")
    first = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("primer.csv", csv_a, "text/csv")},
        follow_redirects=False,
    )
    assert first.headers["location"] == "/t/billing/pending"
    second_page = client.get("/t/billing/pending")
    second = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(second_page.text)},
        files={"csv_file": ("segundo.csv", csv_b, "text/csv")},
        follow_redirects=False,
    )
    assert second.headers["location"] == "/t/billing/pending"

    async def _rows():
        async with db_session() as session:
            result = await session.execute(
                select(BillingExternalConsultation).where(
                    BillingExternalConsultation.tenant_id == tenant_id,
                    BillingExternalConsultation.patient_name == "Andrea Blumtritt",
                )
            )
            return list(result.scalars().all())

    rows = asyncio.run(_rows())
    assert len(rows) == 1
    assert rows[0].professional_name == "Otra profesional"


def test_billing_pending_can_mark_selected_as_no_facturar(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant No Facturar", "whatsapp:+6372"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-no-facturar@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                row = BillingExternalConsultation(
                    tenant_id=tenant_id,
                    external_provider="csv_attended",
                    external_id="no-facturar-1",
                    attended_at=datetime(2026, 6, 17, 3, 0),
                    patient_name="Andrea Blumtritt",
                    patient_document="30123456",
                    patient_email="andrea@example.com",
                    send_email=True,
                    status="pending",
                )
                session.add(row)
                await session.flush()
                return row.id

    row_id = asyncio.run(_seed())
    login(client, "tenant-no-facturar@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    response = client.post(
        "/t/billing/pending/no-facturar",
        data={"csrf_token": _csrf(page.text), "consultation_ids": str(row_id)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            return await session.get(BillingExternalConsultation, row_id)

    row = asyncio.run(_fetch())
    assert row.status == "excluded"
    assert row.send_email is False
    assert row.selected_for_billing is False
    listing = client.get("/t/billing/finalized?status=excluded")
    assert "No facturar" in listing.text
    assert "Andrea Blumtritt" in listing.text


def test_billing_finalized_can_restore_excluded_consultation_to_pending(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Restore Pending", "whatsapp:+6378"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-restore-pending@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                row = BillingExternalConsultation(
                    tenant_id=tenant_id,
                    external_provider="csv_attended",
                    external_id="restore-pending-1",
                    attended_at=datetime(2026, 6, 17, 3, 0),
                    patient_name="Paciente Tardia",
                    patient_document="30999111",
                    patient_email="tardia@example.com",
                    status="excluded",
                    selected_for_billing=False,
                    send_email=False,
                )
                session.add(row)
                await session.flush()
                return row.id

    row_id = asyncio.run(_seed())
    login(client, "tenant-restore-pending@test.com", "secret-123")
    page = client.get("/t/billing/finalized?status=excluded")
    assert "Paciente Tardia" in page.text
    assert "Volver a pendiente" in page.text
    response = client.post(
        f"/t/billing/finalized/{row_id}/restore-pending?date_from=2026-06-17&date_to=2026-06-17",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/t/billing/pending?date_from=2026-06-17&date_to=2026-06-17&status=pending"

    async def _fetch():
        async with db_session() as session:
            return await session.get(BillingExternalConsultation, row_id)

    row = asyncio.run(_fetch())
    assert row.status == "pending"
    assert row.selected_for_billing is False
    assert row.send_email is False
    pending = client.get("/t/billing/pending?status=pending")
    assert "Paciente Tardia" in pending.text


def test_billing_finalized_restore_pending_rejects_billed_consultation(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Restore Billed", "whatsapp:+6379"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-restore-billed@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                row = BillingExternalConsultation(
                    tenant_id=tenant_id,
                    external_provider="csv_attended",
                    external_id="restore-billed-1",
                    attended_at=datetime(2026, 6, 17, 3, 0),
                    patient_name="Paciente Facturada",
                    patient_document="30999222",
                    status="billed",
                )
                session.add(row)
                await session.flush()
                invoice = ArcaInvoice(
                    tenant_id=tenant_id,
                    external_consultation_id=row.id,
                    represented_cuit="27285069012",
                    environment="homo",
                    pto_vta=6,
                    cbte_tipo=11,
                    cbte_nro=101,
                    status=ArcaInvoiceStatus.AUTHORIZED,
                    mon_id="PES",
                )
                session.add(invoice)
                await session.flush()
                row.arca_invoice_id = invoice.id
                return row.id

    row_id = asyncio.run(_seed())
    login(client, "tenant-restore-billed@test.com", "secret-123")
    page = client.get("/t/billing/finalized?status=billed")
    response = client.post(
        f"/t/billing/finalized/{row_id}/restore-pending",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            return await session.get(BillingExternalConsultation, row_id)

    row = asyncio.run(_fetch())
    assert row.status == "billed"
    assert row.arca_invoice_id is not None


def test_billing_emit_keeps_excluded_consultation_without_email(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Excluded No Email", "whatsapp:+6377"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))
    asyncio.run(
        create_user(
            db_session,
            "tenant-excluded-no-email@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _mark_excluded():
        async with db_session() as session:
            async with session.begin():
                row = await session.get(BillingExternalConsultation, consultation_id)
                row.status = "excluded"
                row.selected_for_billing = False
                row.send_email = False

    asyncio.run(_mark_excluded())
    job_started = False

    def _start_job(*args, **kwargs):
        nonlocal job_started
        job_started = True
        raise AssertionError("No debe iniciar job para una consulta marcada como no facturar")

    monkeypatch.setattr("app.web.tenant.views.start_billing_emission_job", _start_job)
    login(client, "tenant-excluded-no-email@test.com", "secret-123")
    page = client.get("/t/billing/pending?status=excluded")
    response = client.post(
        "/t/billing/emit",
        data={
            "csrf_token": _csrf(page.text),
            "consultation_ids": str(consultation_id),
            f"item_id_{consultation_id}": str(item_id),
            f"amount_{consultation_id}": "1500.00",
            f"send_email_{consultation_id}": "on",
            "status": "excluded",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert job_started is False

    async def _fetch():
        async with db_session() as session:
            row = await session.get(BillingExternalConsultation, consultation_id)
            return row.status, row.selected_for_billing, row.send_email

    status, selected_for_billing, send_email = asyncio.run(_fetch())
    assert status == "excluded"
    assert selected_for_billing is False
    assert send_email is False


def test_billing_pending_can_delete_selected_unbilled_consultations(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Delete Pending", "whatsapp:+6373"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-delete-pending@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                row = BillingExternalConsultation(
                    tenant_id=tenant_id,
                    external_provider="csv_attended",
                    external_id="delete-pending-1",
                    attended_at=datetime(2026, 6, 17, 3, 0),
                    patient_name="Andrea Blumtritt",
                    patient_document="30123456",
                    patient_email="andrea@example.com",
                    status="pending",
                )
                session.add(row)
                await session.flush()
                return row.id

    row_id = asyncio.run(_seed())
    login(client, "tenant-delete-pending@test.com", "secret-123")
    page = client.get("/t/billing/pending?status=pending")
    assert "Andrea Blumtritt" in page.text
    response = client.post(
        "/t/billing/pending/delete",
        data={"csrf_token": _csrf(page.text), "consultation_ids": str(row_id), "status": "pending"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            return await session.get(BillingExternalConsultation, row_id)

    assert asyncio.run(_fetch()) is None
    listing = client.get("/t/billing/pending")
    assert "Andrea Blumtritt" not in listing.text


def test_billing_pending_delete_keeps_billed_consultations(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Delete Billed", "whatsapp:+6374"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-delete-billed@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                row = BillingExternalConsultation(
                    tenant_id=tenant_id,
                    external_provider="csv_attended",
                    external_id="delete-billed-1",
                    attended_at=datetime(2026, 6, 17, 3, 0),
                    patient_name="Juan Perez",
                    patient_document="30111222",
                    status="billed",
                )
                session.add(row)
                await session.flush()
                invoice = ArcaInvoice(
                    tenant_id=tenant_id,
                    external_consultation_id=row.id,
                    represented_cuit="27285069012",
                    environment="homo",
                    pto_vta=6,
                    cbte_tipo=11,
                    cbte_nro=99,
                    status=ArcaInvoiceStatus.AUTHORIZED,
                    mon_id="PES",
                )
                session.add(invoice)
                await session.flush()
                row.arca_invoice_id = invoice.id
                return row.id

    row_id = asyncio.run(_seed())
    login(client, "tenant-delete-billed@test.com", "secret-123")
    page = client.get("/t/billing/finalized?status=billed")
    response = client.post(
        "/t/billing/pending/delete",
        data={"csrf_token": _csrf(page.text), "consultation_ids": str(row_id), "status": "billed"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            return await session.get(BillingExternalConsultation, row_id)

    assert asyncio.run(_fetch()) is not None
    listing = client.get("/t/billing/finalized?status=billed")
    assert "Juan Perez" in listing.text


def test_billing_pending_grid_saves_catalog_diagnostic(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Grid Diagnostic", "whatsapp:+6375"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(db_session, tenant_id, diagnosis=None)
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-grid-diagnostic@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _seed_diagnostic():
        async with db_session() as session:
            async with session.begin():
                diagnostic = BillingDiagnostic(
                    tenant_id=tenant_id,
                    code="CONTROL",
                    name="Control ginecologico",
                    active=True,
                )
                session.add(diagnostic)
                await session.flush()
                return diagnostic.id

    diagnostic_id = asyncio.run(_seed_diagnostic())

    class FakeJob:
        id = "fake-diagnostic-job"

    monkeypatch.setattr(
        "app.web.tenant.views.start_billing_emission_job",
        lambda tenant_id_arg, ids: FakeJob(),
    )
    login(client, "tenant-grid-diagnostic@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    assert "Control ginecologico" in page.text
    response = client.post(
        "/t/billing/emit",
        data={
            "csrf_token": _csrf(page.text),
            "consultation_ids": str(consultation_id),
            f"item_id_{consultation_id}": str(item_id),
            f"amount_{consultation_id}": "1500.00",
            f"diagnostic_id_{consultation_id}": str(diagnostic_id),
            f"sale_condition_{consultation_id}": "Transferencia",
            "date_from": "",
            "date_to": "",
            "consultorio_id": "",
            "q": "",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            return consultation.billing_diagnostic_id, consultation.diagnosis, consultation.sale_condition

    saved_diagnostic_id, diagnosis, sale_condition = asyncio.run(_fetch())
    assert saved_diagnostic_id == diagnostic_id
    assert diagnosis == "Control ginecologico"
    assert sale_condition == "Transferencia"


def test_billing_pending_grid_saves_mixed_sale_condition(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Mixed Sale Condition", "whatsapp:+6376"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(db_session, tenant_id, diagnosis="Control")
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-grid-mixed-sale@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    class FakeJob:
        id = "fake-mixed-sale-job"

    monkeypatch.setattr(
        "app.web.tenant.views.start_billing_emission_job",
        lambda tenant_id_arg, ids: FakeJob(),
    )
    login(client, "tenant-grid-mixed-sale@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    response = client.post(
        "/t/billing/emit",
        data={
            "csrf_token": _csrf(page.text),
            "consultation_ids": str(consultation_id),
            f"item_id_{consultation_id}": str(item_id),
            f"amount_{consultation_id}": "1500.00",
            f"sale_condition_{consultation_id}": ["Contado", "Transferencia"],
            "date_from": "",
            "date_to": "",
            "consultorio_id": "",
            "q": "",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            return consultation.sale_condition

    assert asyncio.run(_fetch()) == "Contado / Transferencia"


def test_billing_pending_import_requires_csv_file(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Pending CSV Required", "whatsapp:+634"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-pending-http-error@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-pending-http-error@test.com", "secret-123")

    page = client.get("/t/billing/pending?date_from=2026-07-01&date_to=2026-07-02")
    response = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/t/billing/pending"


def test_billing_pending_starts_consultorio_movil_sync_job(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync CM", "whatsapp:+682"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-sync-cm@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    started = {}

    def fake_start(tenant_id_arg, consultorio_id=None):
        started["tenant_id"] = tenant_id_arg
        started["consultorio_id"] = consultorio_id
        return SimpleNamespace(id="sync-job-test")

    monkeypatch.setattr("app.web.tenant.views.start_billing_consultorio_sync_job", fake_start)
    login(client, "tenant-sync-cm@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    assert "Sincronizar Consultorio Movil" in page.text
    assert "Ultima consulta sincronizada" in page.text
    response = client.post(
        "/t/billing/pending/sync-consultorio-movil",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/t/billing/pending?sync_job_id=sync-job-test"
    assert started == {"tenant_id": tenant_id, "consultorio_id": None}


def test_billing_consultorio_sync_job_status_endpoint(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Status", "whatsapp:+683"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-sync-status@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    class FakeSyncJob:
        def public_dict(self):
            return {"id": "sync-status-job", "status": "running", "percent": 15}

    monkeypatch.setattr(
        "app.web.tenant.views.get_billing_consultorio_sync_job",
        lambda job_id, tenant_id_arg: FakeSyncJob() if job_id == "sync-status-job" and tenant_id_arg == tenant_id else None,
    )
    login(client, "tenant-sync-status@test.com", "secret-123")
    response = client.get("/t/billing/sync-jobs/sync-status-job")
    assert response.status_code == 200
    assert response.json()["status"] == "running"


def test_billing_consultorio_sync_job_reports_blocked_login(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Blocked", "whatsapp:+685"))
    consultorio_id = 9876
    job = BillingConsultorioSyncJob(id="sync-blocked-test", tenant_id=tenant_id, consultorio_id=consultorio_id)
    _jobs[job.id] = job

    async def fake_sync_consultorio(_session, _tenant_id, _consultorio_id):
        return SimpleNamespace(
            id=consultorio_id,
            configuracion_externa={"cabildo": {"user": "u", "password": "p", "staff_id": "77"}},
        )

    async def fake_latest_imported_attended_at(_session, _tenant_id, _consultorio_id):
        return None

    async def fake_sync_state(_session, _tenant_id, _consultorio_id):
        return SimpleNamespace(last_status=None, last_error=None)

    def blocked_login(_username, _password):
        raise ConsultorioMovilAccessBlocked("Consultorio Movil devolvio HTTP 403 al abrir login")

    monkeypatch.setattr("app.services.billing_consultorio_sync_job_service._sync_consultorio", fake_sync_consultorio)
    monkeypatch.setattr(
        "app.services.billing_consultorio_sync_job_service._latest_imported_attended_at",
        fake_latest_imported_attended_at,
    )
    monkeypatch.setattr("app.services.billing_consultorio_sync_job_service._sync_state", fake_sync_state)
    monkeypatch.setattr("app.services.billing_consultorio_sync_job_service.login", blocked_login)

    asyncio.run(_run_billing_consultorio_sync_job(job.id))

    assert job.status == "failed"
    assert job.phase == "Acceso bloqueado por Consultorio Movil"
    assert job.errors == 1
    assert job.error_message == CONSULTORIO_MOVIL_BLOCKED_MESSAGE


def test_billing_consultorio_sync_uses_latest_imported_consultation_date(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Latest", "whatsapp:+684"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Sede Sync Latest",
            proveedor_turnos="consultorio_movil",
            configuracion_externa={"cabildo": {"user": "u", "password": "p", "staff_id": "77"}},
        )
    )

    async def _seed_and_fetch():
        async with db_session() as session:
            async with session.begin():
                session.add_all(
                    [
                        BillingExternalConsultation(
                            tenant_id=tenant_id,
                            consultorio_id=consultorio_id,
                            external_provider="consultorio_movil_sync",
                            external_id="old",
                            attended_at=datetime(2026, 7, 10, 9, 0),
                        ),
                        BillingExternalConsultation(
                            tenant_id=tenant_id,
                            consultorio_id=consultorio_id,
                            external_provider="consultorio_movil_sync",
                            external_id="new",
                            attended_at=datetime(2026, 7, 12, 11, 30),
                        ),
                        BillingExternalConsultation(
                            tenant_id=tenant_id,
                            consultorio_id=consultorio_id,
                            external_provider="csv_attended",
                            external_id="csv-newer",
                            attended_at=datetime(2026, 7, 13, 11, 30),
                        ),
                    ]
                )
            return await _latest_imported_attended_at(session, tenant_id, consultorio_id)

    latest = asyncio.run(_seed_and_fetch())
    assert latest == datetime(2026, 7, 12, 11, 30)


def test_billing_pending_shows_emission_job_error(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Job Error", "whatsapp:+681"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-job-error@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    class FakeJob:
        def public_dict(self):
            return {
                "id": "job-visible-error",
                "tenant_id": tenant_id,
                "status": "completed_with_errors",
                "total": 1,
                "processed": 1,
                "percent": 100,
                "success": 0,
                "failed": 1,
                "emailed": 0,
                "error_message": "Consulta #201: ARCA rechazo la autorizacion",
                "started_at": "",
                "finished_at": "",
            }

    monkeypatch.setattr("app.web.tenant.views.get_billing_emission_job", lambda job_id, current_tenant_id: FakeJob())
    login(client, "tenant-job-error@test.com", "secret-123")

    response = client.get("/t/billing/pending?job_id=job-visible-error")

    assert response.status_code == 200
    assert "Ultimo error: Consulta #201: ARCA rechazo la autorizacion" in response.text


def test_billing_pending_diagnosis_update_and_tenant_scope(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Pending A", "whatsapp:+621"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Pending B", "whatsapp:+622"))
    consultorio_a = asyncio.run(create_consultorio(db_session, tenant_a, "Cabildo A", proveedor_turnos="consultorio_movil"))
    consultorio_b = asyncio.run(create_consultorio(db_session, tenant_b, "Cabildo B", proveedor_turnos="consultorio_movil"))

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                own = BillingExternalConsultation(
                    tenant_id=tenant_a,
                    consultorio_id=consultorio_a,
                    external_provider="consultorio_movil",
                    external_id="own",
                    attended_at=datetime(2026, 7, 3, 9, 0),
                    patient_name="Paciente A",
                    diagnosis="Inicial",
                )
                other = BillingExternalConsultation(
                    tenant_id=tenant_b,
                    consultorio_id=consultorio_b,
                    external_provider="consultorio_movil",
                    external_id="other",
                    attended_at=datetime(2026, 7, 3, 9, 0),
                    patient_name="Paciente B",
                    diagnosis="Otro",
                )
                session.add_all([own, other])
                await session.flush()
                return own.id, other.id

    own_id, other_id = asyncio.run(_seed())
    asyncio.run(
        create_user(
            db_session,
            "tenant-pending-a@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    login(client, "tenant-pending-a@test.com", "secret-123")

    page = client.get("/t/billing/pending?date_from=2026-07-03&date_to=2026-07-03")
    assert "Paciente A" in page.text
    assert "Paciente B" not in page.text
    csrf = _csrf(page.text)

    response = client.post(
        f"/t/billing/pending/{own_id}/diagnosis",
        data={
            "csrf_token": csrf,
            "diagnosis": "Diagnostico editable",
            "date_from": "2026-07-03",
            "date_to": "2026-07-03",
            "consultorio_id": "",
            "q": "",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    forbidden = client.post(
        f"/t/billing/pending/{other_id}/diagnosis",
        data={"csrf_token": csrf, "diagnosis": "No debe"},
    )
    assert forbidden.status_code == 404

    async def _diagnosis():
        async with db_session() as session:
            own = await session.get(BillingExternalConsultation, own_id)
            other = await session.get(BillingExternalConsultation, other_id)
            return own.diagnosis, other.diagnosis

    own_diag, other_diag = asyncio.run(_diagnosis())
    assert own_diag == "Diagnostico editable"
    assert other_diag == "Otro"


def test_arca_service_emits_invoice_with_diagnosis(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit OK", "whatsapp:+626"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Juan",
            apellido="Perez",
            dni="30111222",
            tipo_documento="DNI",
            numero_documento="30111222",
            document_number_normalized="30111222",
            email="juan@example.com",
            obra_social="OSDE",
            insurance_number="123456789",
        )
    )
    captured = {}

    class FakeWsfe:
        def __init__(self, settings, auth_provider):
            self.auth_provider = auth_provider

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            captured["auth"] = self.auth_provider()
            captured["scope"] = (pto_vta, cbte_tipo)
            return WsfeResult(data={"CbteNro": 10})

        def solicitar_cae(self, request):
            captured["request"] = request
            return WsfeResult(
                data={
                    "FeCabResp": {"Resultado": "A"},
                    "FeDetResp": {
                        "FEDetResponse": {
                            "Resultado": "A",
                            "CAE": "12345678901234",
                            "CAEFchVto": "20260714",
                            "CbteDesde": 11,
                            "CbteHasta": 11,
                        }
                    },
                }
            )

        def consultar_comprobante(self, pto_vta, cbte_tipo, cbte_nro):
            raise AssertionError("No debe recuperar cuando FECAESolicitar autoriza")

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            item = await session.get(ArcaBillableItem, item_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            consultation.sale_condition = "Transferencia"
            await session.flush()
            service = ArcaService(
                session,
                wsaa_factory=_FakeWsaaForEmission,
                wsfe_factory=FakeWsfe,
            )
            result = await service.emit_invoice_for_consultation(tenant, consultation, item)
            await session.commit()
            return result.invoice.id, result.recovered

    invoice_id, recovered = asyncio.run(_run())
    assert recovered is False
    assert captured["auth"]["Token"] == "token"
    assert captured["scope"] == (3, 11)
    detail = captured["request"]["FeDetReq"]["FECAEDetRequest"][0]
    assert "metadata" not in captured["request"]
    assert "Diagnostico" not in detail
    assert "Descripcion" not in detail
    assert detail["CondicionIVAReceptorId"] == 5

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            line = await session.scalar(
                select(BillingInvoiceLine).where(BillingInvoiceLine.invoice_id == invoice_id)
            )
            return invoice, consultation, line

    invoice, consultation, line = asyncio.run(_fetch())
    assert invoice.status == ArcaInvoiceStatus.AUTHORIZED
    assert invoice.cbte_nro == 11
    assert invoice.cae == "12345678901234"
    assert invoice.cae_fch_vto.isoformat() == "2026-07-14"
    assert invoice.external_consultation_id == consultation_id
    assert invoice.billing_item_id == item_id
    assert invoice.request_json["metadata"]["diagnosis"] == "Bronquitis aguda"
    assert invoice.request_json["metadata"]["description"] == "Consulta medica - OSDE - Afiliado 123456789"
    assert invoice.request_json["metadata"]["insurance_name"] == "OSDE"
    assert invoice.request_json["metadata"]["insurance_number"] == "123456789"
    assert invoice.request_json["metadata"]["sale_condition"] == "Transferencia"
    assert invoice.request_json["metadata"]["receiver_tax_condition_id"] == 5
    assert invoice.diagnosis_original_snapshot == "Bronquitis aguda"
    assert invoice.diagnosis_final_snapshot == "Bronquitis aguda"
    assert consultation.arca_invoice_id == invoice_id
    assert consultation.status == "billed"
    assert line.description == "Consulta medica - OSDE - Afiliado 123456789"
    assert line.diagnosis_text == "Bronquitis aguda"


def test_arca_service_emits_invoice_without_diagnosis(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit No Diagnosis", "whatsapp:+646"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(db_session, tenant_id, diagnosis=None)
    )
    captured = {}

    class FakeWsfe:
        def __init__(self, settings, auth_provider):
            self.auth_provider = auth_provider

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            return WsfeResult(data={"CbteNro": 20})

        def solicitar_cae(self, request):
            captured["request"] = request
            return WsfeResult(
                data={
                    "FeCabResp": {"Resultado": "A"},
                    "FeDetResp": {
                        "FEDetResponse": {
                            "Resultado": "A",
                            "CAE": "22345678901234",
                            "CAEFchVto": "20260714",
                        }
                    },
                }
            )

        def consultar_comprobante(self, pto_vta, cbte_tipo, cbte_nro):
            raise AssertionError("No debe recuperar cuando FECAESolicitar autoriza")

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            item = await session.get(ArcaBillableItem, item_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            result = await ArcaService(
                session,
                wsaa_factory=_FakeWsaaForEmission,
                wsfe_factory=FakeWsfe,
            ).emit_invoice_for_consultation(tenant, consultation, item)
            await session.commit()
            return result.invoice.id

    invoice_id = asyncio.run(_run())
    detail = captured["request"]["FeDetReq"]["FECAEDetRequest"][0]
    assert "metadata" not in captured["request"]
    assert "Diagnostico" not in detail
    assert "Descripcion" not in detail

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            line = await session.scalar(
                select(BillingInvoiceLine).where(BillingInvoiceLine.invoice_id == invoice_id)
            )
            return invoice, line

    invoice, line = asyncio.run(_fetch())
    assert invoice.status == ArcaInvoiceStatus.AUTHORIZED
    assert invoice.request_json["metadata"]["diagnosis"] == ""
    assert invoice.request_json["metadata"]["description"] == "Consulta medica - OSDE"
    assert invoice.diagnosis_original_snapshot == ""
    assert invoice.diagnosis_final_snapshot == ""
    assert line.description == "Consulta medica - OSDE"
    assert line.diagnosis_text == ""


def test_arca_service_authorizes_with_fecae_detail_response_and_events(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit Event OK", "whatsapp:+683"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))

    class EventWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            return WsfeResult(data={"CbteNro": 40})

        def solicitar_cae(self, request):
            return WsfeResult(
                data={
                    "FeCabResp": {"Resultado": "A"},
                    "FeDetResp": {
                        "FECAEDetResponse": [
                            {
                                "Resultado": "A",
                                "CAE": "32345678901234",
                                "CAEFchVto": "20260714",
                            }
                        ]
                    },
                    "Events": {
                        "Evt": [
                            {
                                "Code": 39,
                                "Msg": "IMPORTANTE: campo Condicion Frente al IVA del receptor.",
                            }
                        ]
                    },
                }
            )

        def consultar_comprobante(self, pto_vta, cbte_tipo, cbte_nro):
            raise AssertionError("No debe recuperar cuando FECAESolicitar autoriza")

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            item = await session.get(ArcaBillableItem, item_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            result = await ArcaService(
                session,
                wsaa_factory=_FakeWsaaForEmission,
                wsfe_factory=EventWsfe,
            ).emit_invoice_for_consultation(tenant, consultation, item)
            await session.commit()
            return result.invoice.id

    invoice_id = asyncio.run(_run())

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            return invoice, consultation

    invoice, consultation = asyncio.run(_fetch())
    assert invoice.status == ArcaInvoiceStatus.AUTHORIZED
    assert invoice.cae == "32345678901234"
    assert invoice.error_message in (None, "")
    assert consultation.status == "billed"


def test_arca_service_persists_rejected_invoice_on_arca_error(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit Error", "whatsapp:+627"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))

    class FailingWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            return WsfeResult(data={"CbteNro": 20})

        def solicitar_cae(self, request):
            raise WsfeError("ARCA rechazo la solicitud")

        def consultar_comprobante(self, pto_vta, cbte_tipo, cbte_nro):
            raise WsfeError("Comprobante inexistente")

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            item = await session.get(ArcaBillableItem, item_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            service = ArcaService(
                session,
                wsaa_factory=_FakeWsaaForEmission,
                wsfe_factory=FailingWsfe,
            )
            message = ""
            try:
                await service.emit_invoice_for_consultation(tenant, consultation, item)
            except ArcaEmissionError as exc:
                message = str(exc)
            await session.commit()
            return message

    message = asyncio.run(_run())
    assert "ARCA rechazo" in message

    async def _fetch():
        async with db_session() as session:
            invoice = await session.scalar(
                select(ArcaInvoice).where(ArcaInvoice.tenant_id == tenant_id)
            )
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            return invoice, consultation.arca_invoice_id

    invoice, linked_invoice_id = asyncio.run(_fetch())
    assert invoice.status == ArcaInvoiceStatus.REJECTED
    assert invoice.cbte_nro == 21
    assert invoice.error_message == "ARCA rechazo la solicitud"
    assert linked_invoice_id is None


def test_arca_service_surfaces_arca_observations_on_rejection(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit Observed", "whatsapp:+682"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))

    class RejectedWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            return WsfeResult(data={"CbteNro": 30})

        def solicitar_cae(self, request):
            return WsfeResult(
                data={
                    "FeCabResp": {"Resultado": "R"},
                    "FeDetResp": {
                        "FEDetResponse": {
                            "Resultado": "R",
                            "Obs": {
                                "Observaciones": [
                                    {
                                        "Code": 10246,
                                        "Msg": "Campo Condicion Frente al IVA del receptor es obligatorio.",
                                    }
                                ]
                            },
                        }
                    },
                }
            )

        def consultar_comprobante(self, pto_vta, cbte_tipo, cbte_nro):
            raise AssertionError("No debe recuperar si FECAESolicitar respondio rechazo controlado")

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            item = await session.get(ArcaBillableItem, item_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            try:
                await ArcaService(
                    session,
                    wsaa_factory=_FakeWsaaForEmission,
                    wsfe_factory=RejectedWsfe,
                ).emit_invoice_for_consultation(tenant, consultation, item)
            except ArcaEmissionError as exc:
                message = str(exc)
            else:
                raise AssertionError("Debia rechazar")
            await session.commit()
            invoice = await session.scalar(
                select(ArcaInvoice).where(ArcaInvoice.external_consultation_id == consultation_id)
            )
            return message, invoice.error_message

    message, invoice_error = asyncio.run(_run())

    assert "10246" in message
    assert "Condicion Frente al IVA" in message
    assert invoice_error == message


def test_arca_service_blocks_double_billing(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit Twice", "whatsapp:+628"))
    invoice_id = asyncio.run(
        _create_invoice(db_session, tenant_id, cbte_nro=31, amount=Decimal("100.00"))
    )
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(db_session, tenant_id, arca_invoice_id=invoice_id)
    )

    class UnexpectedWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            raise AssertionError("No debe llamar ARCA si la consulta ya esta facturada")

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            item = await session.get(ArcaBillableItem, item_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            service = ArcaService(
                session,
                wsaa_factory=_FakeWsaaForEmission,
                wsfe_factory=UnexpectedWsfe,
            )
            try:
                await service.emit_invoice_for_consultation(tenant, consultation, item)
            except ArcaInvoiceAlreadyExists as exc:
                return str(exc)
            return ""

    message = asyncio.run(_run())
    assert "ya esta facturada" in message


def test_billing_csv_import_crosses_with_local_billed_consultations(
    client,
    db_session,
    monkeypatch,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cross Billed", "whatsapp:+635"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Sede Cross",
            proveedor_turnos="consultorio_movil",
            configuracion_externa={
                "cabildo": {
                    "user": "cm-user",
                    "password": "cm-pass",
                    "staff_id": "77",
                }
            },
        )
    )
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))
    invoice_id = asyncio.run(_emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id))
    asyncio.run(
        create_user(
            db_session,
            "tenant-cross-billed@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _billed_external_id():
        async with db_session() as session:
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            consultation.consultorio_id = consultorio_id
            await session.commit()
            return consultation.external_id

    external_id = asyncio.run(_billed_external_id())
    login(client, "tenant-cross-billed@test.com", "secret-123")

    pending_before = client.get("/t/billing/pending")
    assert "Juan Perez" not in pending_before.text
    finalized_before = client.get("/t/billing/finalized?status=billed")
    assert "Juan Perez" in finalized_before.text
    assert "Facturado" in finalized_before.text
    csv_content = (
        "id,fecha,paciente,email,obra social,profesional,practica,diagnostico\n"
        f"{external_id},04/07/2026 10:00,Juan Perez,juan@example.com,OSDE,Dra Gomez,Consulta,Bronquitis aguda\n"
    ).encode("utf-8-sig")

    response = client.post(
        "/t/billing/pending/import",
        data={"csrf_token": _csrf(pending_before.text)},
        files={"csv_file": ("atendidas.csv", csv_content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    pending_after = client.get("/t/billing/pending?date_from=2026-07-04&date_to=2026-07-04")
    assert "Juan Perez" not in pending_after.text
    finalized_after = client.get("/t/billing/finalized?date_from=2026-07-04&date_to=2026-07-04&status=billed")
    assert "Juan Perez" in finalized_after.text
    assert "Facturado" in finalized_after.text
    assert "04/07/2026" in finalized_after.text

    invoice_list = client.get("/t/billing/invoices")
    assert invoice_list.status_code == 200
    assert "Fecha facturacion" in invoice_list.text
    assert external_id not in invoice_list.text
    assert "No enviado" in invoice_list.text

    async def _verify_single_billed_link():
        async with db_session() as session:
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            invoices = await session.execute(
                select(ArcaInvoice).where(ArcaInvoice.tenant_id == tenant_id)
            )
            return consultation.arca_invoice_id, consultation.status, consultation.billed_at, len(list(invoices.scalars().all()))

    linked_invoice_id, status, billed_at, invoice_count = asyncio.run(_verify_single_billed_link())
    assert linked_invoice_id == invoice_id
    assert status == "billed"
    assert billed_at is not None
    assert invoice_count == 1


def test_arca_service_recovers_invoice_with_fe_comp_consultar(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit Recover", "whatsapp:+629"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))
    calls = {"consultar": 0}

    class RecoveringWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            return WsfeResult(data={"CbteNro": 40})

        def solicitar_cae(self, request):
            raise WsfeError("Timeout al autorizar")

        def consultar_comprobante(self, pto_vta, cbte_tipo, cbte_nro):
            calls["consultar"] += 1
            return WsfeResult(
                data={
                    "Resultado": "A",
                    "CodAutorizacion": "99999999999999",
                    "FchVto": "20260715",
                }
            )

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            item = await session.get(ArcaBillableItem, item_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            service = ArcaService(
                session,
                wsaa_factory=_FakeWsaaForEmission,
                wsfe_factory=RecoveringWsfe,
            )
            result = await service.emit_invoice_for_consultation(tenant, consultation, item)
            await session.commit()
            return result.invoice.id, result.recovered

    invoice_id, recovered = asyncio.run(_run())
    assert recovered is True
    assert calls["consultar"] == 1

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            return invoice, consultation.arca_invoice_id

    invoice, linked_invoice_id = asyncio.run(_fetch())
    assert invoice.status == ArcaInvoiceStatus.AUTHORIZED
    assert invoice.cae == "99999999999999"
    assert invoice.cae_fch_vto.isoformat() == "2026-07-15"
    assert linked_invoice_id == invoice_id


def test_billing_preview_allows_missing_diagnosis(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Preview Diagnosis", "whatsapp:+630"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(db_session, tenant_id, diagnosis=None)
    )

    async def _prepare_consultation():
        async with db_session() as session:
            async with session.begin():
                consultation = await session.get(BillingExternalConsultation, consultation_id)
                consultation.billing_item_id = item_id
                consultation.amount = Decimal("1500.00")

    asyncio.run(_prepare_consultation())
    asyncio.run(
        create_user(
            db_session,
            "tenant-preview-diagnosis@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-preview-diagnosis@test.com", "secret-123")
    page = client.get("/t/billing/pending")
    assert page.status_code == 200
    response = client.post(
        "/t/billing/preview",
        data={
            "csrf_token": _csrf(page.text),
            "consultation_ids": str(consultation_id),
            "date_from": "",
            "date_to": "",
            "consultorio_id": "",
            "q": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "No informado" in response.text
    assert "Opcional" in response.text


def test_billing_invoice_document_html_and_pdf_include_diagnosis(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Document", "whatsapp:+631"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Diagnostico visible obligatorio",
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )

    async def _build():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            invoice = await session.get(ArcaInvoice, invoice_id)
            consultation = await session.get(BillingExternalConsultation, consultation_id)
            consultation.sale_condition = "Otros medios"
            return await BillingInvoiceDocumentService(session).build_document(tenant, invoice)

    document = asyncio.run(_build())
    assert "Diagnostico visible obligatorio" in document.html
    assert "Condicion de venta: Otros medios" in document.html
    assert document.pdf.startswith(b"%PDF")
    assert b"Diagnostico" in document.pdf
    assert document.patient_email == "paciente@example.com"


def test_billing_invoice_document_is_not_stored_until_requested(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Deferred Document", "whatsapp:+685"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Diagnostico almacenado",
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            return (
                invoice.document_html,
                invoice.document_pdf,
                invoice.document_filename,
                invoice.document_generated_at,
            )

    document_html, document_pdf, filename, generated_at = asyncio.run(_fetch())
    assert document_html is None
    assert document_pdf is None
    assert filename is None
    assert generated_at is None


def test_billing_invoice_document_includes_activity_start_date(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Activity Start", "whatsapp:+68501"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Diagnostico con inicio de actividades",
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )

    async def _build():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            tenant.arca_settings = {
                **(tenant.arca_settings or {}),
                "fiscal_name": "Consultorio Activity",
                "fiscal_address": "Calle Fiscal 123",
                "activity_start_date": "2026-07-01",
            }
            invoice = await session.get(ArcaInvoice, invoice_id)
            return await BillingInvoiceDocumentService(session).build_document(tenant, invoice)

    document = asyncio.run(_build())
    assert "Inicio de actividades: 01/07/2026" in document.html
    assert b"Inicio de actividades" in document.pdf
    assert b"01/07/2026" in document.pdf


def test_billing_invoice_document_includes_configured_professional_legend(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Professional Legend", "whatsapp:+68502"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Diagnostico con leyenda profesional",
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )
    legend = "Medica especialista en Ginecologia y Obstetricia M.N. 122.674"

    async def _build():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            tenant.arca_settings = {
                **(tenant.arca_settings or {}),
                "professional_legend": legend,
            }
            invoice = await session.get(ArcaInvoice, invoice_id)
            return await BillingInvoiceDocumentService(session).build_document(tenant, invoice)

    document = asyncio.run(_build())
    assert legend.encode("latin-1") in document.pdf


def test_billing_invoice_generate_pdf_route_stores_document(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Generate PDF", "whatsapp:+6851"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Diagnostico generado bajo demanda",
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-generate-pdf@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-generate-pdf@test.com", "secret-123")
    detail = client.get(f"/t/billing/invoices/{invoice_id}")
    response = client.post(
        f"/t/billing/invoices/{invoice_id}/generate-pdf",
        data={"csrf_token": _csrf(detail.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            return invoice

    invoice = asyncio.run(_fetch())
    assert invoice.document_pdf.startswith(b"%PDF")
    assert "Diagnostico generado bajo demanda" in invoice.document_html
    assert invoice.pdf_generated_at is not None
    assert invoice.pdf_path
    assert invoice.qr_url and "afip.gob.ar/fe/qr" in invoice.qr_url


def test_billing_invoice_generate_pdf_route_always_rebuilds_document(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Force Rebuild PDF", "whatsapp:+68511"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Diagnostico regenerado completo",
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )

    async def _seed_stale_document():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            tenant.arca_settings = {
                **(tenant.arca_settings or {}),
                "activity_start_date": "2026-07-01",
            }
            invoice = await session.get(ArcaInvoice, invoice_id)
            invoice.document_html = "<html>documento viejo</html>"
            invoice.document_pdf = b"%PDF-DOCUMENTO-VIEJO"
            invoice.document_filename = "factura-vieja.pdf"
            await session.commit()

    asyncio.run(_seed_stale_document())
    asyncio.run(
        create_user(
            db_session,
            "tenant-force-rebuild-pdf@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-force-rebuild-pdf@test.com", "secret-123")
    detail = client.get(f"/t/billing/invoices/{invoice_id}")
    response = client.post(
        f"/t/billing/invoices/{invoice_id}/generate-pdf",
        data={"csrf_token": _csrf(detail.text)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            return invoice.document_html, bytes(invoice.document_pdf)

    document_html, document_pdf = asyncio.run(_fetch())
    assert "documento viejo" not in document_html
    assert "Diagnostico regenerado completo" in document_html
    assert document_pdf != b"%PDF-DOCUMENTO-VIEJO"
    assert b"Inicio de actividades" in document_pdf
    assert b"01/07/2026" in document_pdf


def test_billing_invoice_document_allows_missing_diagnosis(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Document No Diagnosis", "whatsapp:+647"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis=None,
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )

    async def _build():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            invoice = await session.get(ArcaInvoice, invoice_id)
            return await BillingInvoiceDocumentService(session).build_document(tenant, invoice)

    document = asyncio.run(_build())
    assert "No informado" in document.html
    assert document.pdf.startswith(b"%PDF")
    assert document.patient_email == "paciente@example.com"


def test_billing_invoice_email_sends_pdf_and_logs(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Email", "whatsapp:+632"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Laringitis aguda",
            patient_email="paciente@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )
    sent = {}

    class FakeMailer:
        def send_email(self, to_email, subject, body, *, html_body=None, attachments=None):
            sent["to_email"] = to_email
            sent["subject"] = subject
            sent["body"] = body
            sent["html_body"] = html_body
            sent["attachments"] = attachments

    async def _send():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            tenant.arca_settings = {
                **(tenant.arca_settings or {}),
                "email_subject_template": "Factura {numero} - {importe} {moneda}",
                "email_body_template": "Adjuntamos la factura {numero}. CAE {cae}.",
            }
            invoice = await session.get(ArcaInvoice, invoice_id)
            document = await BillingInvoiceDocumentService(session).build_document(tenant, invoice)
            log = await BillingInvoiceEmailService(session, mailer=FakeMailer()).send_invoice(
                tenant,
                invoice,
                to_email=document.patient_email,
                document=document,
            )
            await session.commit()
            return log.id

    log_id = asyncio.run(_send())
    assert sent["to_email"] == "paciente@example.com"
    assert sent["subject"].startswith("Factura ")
    assert "1500.00 PES" in sent["subject"]
    assert "CAE" in sent["body"]
    assert "Laringitis aguda" in sent["html_body"]
    assert "Factura electronica" in sent["html_body"]
    assert "El comprobante fiscal se encuentra adjunto" in sent["html_body"]
    assert sent["attachments"][0][0].endswith(".pdf")
    assert sent["attachments"][0][1].startswith(b"%PDF")

    async def _fetch_log():
        async with db_session() as session:
            return await session.get(BillingEmailLog, log_id)

    log = asyncio.run(_fetch_log())
    assert log.status == "sent"
    assert log.recipient_email == "paciente@example.com"
    assert log.sent_at is not None


def test_messaging_service_raises_when_smtp_missing():
    service = MessagingService()
    service._settings = SimpleNamespace(
        smtp_host=None,
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_from_email=None,
        smtp_from_name=None,
        smtp_use_tls=True,
        email_provider=None,
        email_from=None,
        resend_api_key=None,
        app_name="test",
    )

    with pytest.raises(RuntimeError, match="SMTP no configurado"):
        service.send_email("paciente@example.com", "Factura", "Body")


def test_messaging_service_uses_resend_api_key(monkeypatch):
    calls = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"id": "email_123"}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        calls["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    service = MessagingService()
    service._settings = SimpleNamespace(
        smtp_host=None,
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_from_email=None,
        smtp_from_name="Facturacion",
        smtp_use_tls=True,
        email_provider="resend",
        email_from="Nubelio - Factura electronica <facturacion@nubelio.app>",
        resend_api_key="re_test_key",
        app_name="test",
    )

    service.send_email(
        "paciente@example.com",
        "Factura",
        "Body",
        html_body="<p>Body</p>",
        attachments=[("factura.pdf", b"%PDF", "application/pdf")],
    )

    assert calls["url"] == "https://api.resend.com/emails"
    assert calls["headers"]["Authorization"] == "Bearer re_test_key"
    assert calls["timeout"] == 20
    assert calls["json"]["from"] == "Nubelio - Factura electronica <facturacion@nubelio.app>"
    assert calls["json"]["to"] == ["paciente@example.com"]
    assert calls["json"]["subject"] == "Factura"
    assert calls["json"]["text"] == "Body"
    assert calls["json"]["html"] == "<p>Body</p>"
    assert calls["json"]["attachments"][0]["filename"] == "factura.pdf"
    assert calls["json"]["attachments"][0]["content"] == "JVBERg=="


def test_billing_invoice_email_logs_failed_mailer(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Email Fail", "whatsapp:+684"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Rinitis",
            patient_email="paciente-fail@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )

    class FailingMailer:
        def send_email(self, to_email, subject, body, *, html_body=None, attachments=None):
            raise RuntimeError("SMTP no configurado. Falta SMTP_HOST.")

    async def _send():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            invoice = await session.get(ArcaInvoice, invoice_id)
            document = await BillingInvoiceDocumentService(session).build_document(tenant, invoice)
            try:
                await BillingInvoiceEmailService(session, mailer=FailingMailer()).send_invoice(
                    tenant,
                    invoice,
                    to_email=document.patient_email,
                    document=document,
                )
            except Exception as exc:
                message = str(exc)
            else:
                raise AssertionError("Debia fallar")
            await session.commit()
            log = await session.scalar(
                select(BillingEmailLog).where(BillingEmailLog.invoice_id == invoice_id)
            )
            invoice = await session.get(ArcaInvoice, invoice_id)
            return message, log, invoice

    message, log, invoice = asyncio.run(_send())
    assert "SMTP no configurado" in message
    assert log.status == "failed"
    assert "SMTP no configurado" in log.error_message
    assert invoice.email_sent_at is None


def test_billing_invoice_send_email_route_uses_patient_email(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Email Route", "whatsapp:+633"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="Rinitis alergica",
            patient_email="paciente-route@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-email-route@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    sent = {}

    def fake_send_email(self, to_email, subject, body, *, html_body=None, attachments=None):
        sent["to_email"] = to_email
        sent["body"] = body
        sent["html_body"] = html_body
        sent["attachments"] = attachments

    monkeypatch.setattr("app.services.messaging_service.MessagingService.send_email", fake_send_email)
    login(client, "tenant-email-route@test.com", "secret-123")
    detail = client.get(f"/t/billing-arca/{invoice_id}")
    assert detail.status_code == 200
    assert "Rinitis alergica" in detail.text

    response = client.post(
        f"/t/billing-arca/{invoice_id}/send-email",
        data={
            "csrf_token": _csrf(detail.text),
            "to_email": "",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert sent["to_email"] == "paciente-route@example.com"
    assert "Rinitis alergica" in sent["body"]
    assert "Rinitis alergica" in sent["html_body"]
    assert sent["attachments"][0][1].startswith(b"%PDF")


def test_billing_invoice_pdf_route_regenerates_stale_stored_document(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Stored PDF Route", "whatsapp:+686"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(
            db_session,
            tenant_id,
            diagnosis="PDF persistido",
            patient_email="paciente-route@example.com",
        )
    )
    invoice_id = asyncio.run(
        _emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id)
    )

    async def _replace_pdf():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            tenant.arca_settings = {
                **(tenant.arca_settings or {}),
                "activity_start_date": "2026-07-01",
            }
            invoice = await session.get(ArcaInvoice, invoice_id)
            invoice.document_pdf = b"%PDF-STORED Inicio de actividades"
            invoice.document_html = "<html>Inicio de actividades</html>"
            invoice.document_filename = "factura-almacenada.pdf"
            await session.commit()

    asyncio.run(_replace_pdf())
    asyncio.run(
        create_user(
            db_session,
            "tenant-pdf-route@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-pdf-route@test.com", "secret-123")
    response = client.get(f"/t/billing-arca/{invoice_id}/comprobante.pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert response.content != b"%PDF-STORED Inicio de actividades"
    assert b"Inicio de actividades" in response.content
    assert b"01/07/2026" in response.content
    assert response.headers["content-type"] == "application/pdf"
    assert "factura-almacenada.pdf" in response.headers["content-disposition"]


def test_manual_invoices_can_be_filtered_and_show_actionable_delivery_status(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Manual Listing", "whatsapp:+687"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))
    asyncio.run(_emit_authorized_test_invoice(db_session, tenant_id, item_id, consultation_id))

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                missing_email = ArcaInvoice(
                    tenant_id=tenant_id,
                    origin="manual",
                    receiver_name_snapshot="Receptor sin email",
                    represented_cuit="20123456789",
                    environment="prod",
                    pto_vta=1,
                    cbte_tipo=11,
                    cbte_nro=71,
                    concepto=2,
                    doc_tipo=96,
                    doc_nro="30111222",
                    imp_total=Decimal("1200.00"),
                    imp_tot_conc=Decimal("0.00"),
                    imp_neto=Decimal("1200.00"),
                    imp_op_ex=Decimal("0.00"),
                    imp_trib=Decimal("0.00"),
                    imp_iva=Decimal("0.00"),
                    mon_id="PES",
                    mon_cotiz=Decimal("1.000000"),
                    status=ArcaInvoiceStatus.AUTHORIZED,
                    cae="12345678901234",
                )
                failed_delivery = ArcaInvoice(
                    tenant_id=tenant_id,
                    origin="manual",
                    receiver_name_snapshot="Receptor con entrega fallida",
                    represented_cuit="20123456789",
                    environment="prod",
                    pto_vta=1,
                    cbte_tipo=11,
                    cbte_nro=72,
                    concepto=2,
                    doc_tipo=96,
                    doc_nro="30222333",
                    imp_total=Decimal("1300.00"),
                    imp_tot_conc=Decimal("0.00"),
                    imp_neto=Decimal("1300.00"),
                    imp_op_ex=Decimal("0.00"),
                    imp_trib=Decimal("0.00"),
                    imp_iva=Decimal("0.00"),
                    mon_id="PES",
                    mon_cotiz=Decimal("1.000000"),
                    status=ArcaInvoiceStatus.AUTHORIZED,
                    cae="22345678901234",
                    email_to="fallido@example.com",
                )
                session.add_all([missing_email, failed_delivery])
                await session.flush()
                session.add(
                    BillingEmailLog(
                        tenant_id=tenant_id,
                        invoice_id=failed_delivery.id,
                        recipient_email="fallido@example.com",
                        subject="Factura",
                        status="failed",
                        error_message="SMTP no configurado",
                    )
                )

    asyncio.run(_seed())
    asyncio.run(create_user(db_session, "tenant-manual-list@test.com", hash_password("secret-123"), UserRole.TENANT_ADMIN.value, tenant_id))
    login(client, "tenant-manual-list@test.com", "secret-123")

    response = client.get("/t/billing/invoices?origin=manual")

    assert response.status_code == 200
    assert "Factura manual" in response.text
    assert "Receptor sin email" in response.text
    assert "Receptor con entrega fallida" in response.text
    assert "Sin email configurado" in response.text
    assert "Entrega fallida" in response.text
    assert "Juan Perez" not in response.text


def test_manual_authorized_invoice_can_be_resent_without_requesting_another_cae(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Manual Resend", "whatsapp:+688"))

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.arca_settings = {"fiscal_name": "Consultorio", "fiscal_address": "Calle 1"}
                invoice = ArcaInvoice(
                    tenant_id=tenant_id,
                    origin="manual",
                    receiver_name_snapshot="Receptor de reenvio",
                    represented_cuit="20123456789",
                    environment="prod",
                    pto_vta=1,
                    cbte_tipo=11,
                    cbte_nro=73,
                    concepto=2,
                    doc_tipo=96,
                    doc_nro="30333444",
                    imp_total=Decimal("1400.00"),
                    imp_tot_conc=Decimal("0.00"),
                    imp_neto=Decimal("1400.00"),
                    imp_op_ex=Decimal("0.00"),
                    imp_trib=Decimal("0.00"),
                    imp_iva=Decimal("0.00"),
                    mon_id="PES",
                    mon_cotiz=Decimal("1.000000"),
                    status=ArcaInvoiceStatus.AUTHORIZED,
                    cae="32345678901234",
                    cae_fch_vto=datetime(2026, 7, 31).date(),
                    email_to="receptor@example.com",
                    request_json={"metadata": {"description": "Prestacion manual"}},
                )
                session.add(invoice)
                await session.flush()
                return invoice.id, invoice.cae, invoice.cbte_nro

    invoice_id, cae, cbte_nro = asyncio.run(_seed())
    asyncio.run(create_user(db_session, "tenant-manual-resend@test.com", hash_password("secret-123"), UserRole.TENANT_ADMIN.value, tenant_id))
    sent = {}

    def fake_send_email(self, to_email, subject, body, *, html_body=None, attachments=None):
        sent["to_email"] = to_email
        sent["attachments"] = attachments

    monkeypatch.setattr("app.services.messaging_service.MessagingService.send_email", fake_send_email)
    login(client, "tenant-manual-resend@test.com", "secret-123")
    detail = client.get(f"/t/billing-arca/{invoice_id}")
    response = client.post(
        f"/t/billing-arca/{invoice_id}/send-email",
        data={"csrf_token": _csrf(detail.text), "to_email": ""},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert sent["to_email"] == "receptor@example.com"
    assert sent["attachments"][0][1].startswith(b"%PDF")

    async def _fetch():
        async with db_session() as session:
            invoice = await session.get(ArcaInvoice, invoice_id)
            return invoice.cae, invoice.cbte_nro, invoice.email_sent_at

    persisted_cae, persisted_number, emailed_at = asyncio.run(_fetch())
    assert persisted_cae == cae
    assert persisted_number == cbte_nro
    assert emailed_at is not None


def test_manual_rejection_is_visible_as_history_and_starts_a_new_corrected_invoice(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Manual Rejection", "whatsapp:+689"))

    async def _seed():
        async with db_session() as session:
            async with session.begin():
                invoice = ArcaInvoice(
                    tenant_id=tenant_id,
                    origin="manual",
                    receiver_name_snapshot="Receptor rechazado",
                    represented_cuit="20123456789",
                    environment="prod",
                    pto_vta=1,
                    cbte_tipo=11,
                    cbte_nro=74,
                    concepto=2,
                    doc_tipo=96,
                    doc_nro="30444555",
                    imp_total=Decimal("1500.00"),
                    imp_tot_conc=Decimal("0.00"),
                    imp_neto=Decimal("1500.00"),
                    imp_op_ex=Decimal("0.00"),
                    imp_trib=Decimal("0.00"),
                    imp_iva=Decimal("0.00"),
                    mon_id="PES",
                    mon_cotiz=Decimal("1.000000"),
                    status=ArcaInvoiceStatus.REJECTED,
                    error_message="ARCA rechazo la autorizacion: documento invalido",
                )
                session.add(invoice)
                await session.flush()
                session.add(
                    ArcaInvoiceEvent(
                        invoice_id=invoice.id,
                        event_type="authorization_rejected",
                        payload_json={"error": invoice.error_message, "origin": "manual"},
                    )
                )
                return invoice.id

    invoice_id = asyncio.run(_seed())
    asyncio.run(create_user(db_session, "tenant-manual-rejected@test.com", hash_password("secret-123"), UserRole.TENANT_ADMIN.value, tenant_id))
    login(client, "tenant-manual-rejected@test.com", "secret-123")

    response = client.get(f"/t/billing-arca/{invoice_id}")

    assert response.status_code == 200
    assert "Rechazo de ARCA" in response.text
    assert "documento invalido" in response.text
    assert "Nueva factura corregida" in response.text


def test_manual_arca_rejection_persists_an_immutable_rejection_event(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Manual Rejection Event", "whatsapp:+690"))
    item_id, _ = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "whatsapp:+549110000690",
            dni="30555666",
            iva_condition="Consumidor Final",
        )
    )

    class RejectedWsfe:
        def __init__(self, settings, auth_provider):
            pass

        def get_ultimo_autorizado(self, pto_vta, cbte_tipo):
            return WsfeResult(data={"CbteNro": 80})

        def solicitar_cae(self, request):
            return WsfeResult(
                data={
                    "FeCabResp": {"Resultado": "R"},
                    "FeDetResp": {"FEDetResponse": {"Resultado": "R"}},
                }
            )

    async def _run():
        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            tenant.arca_settings = {**(tenant.arca_settings or {}), "environment": "prod"}
            patient = await session.get(Paciente, patient_id)
            item = await session.get(ArcaBillableItem, item_id)
            with pytest.raises(ArcaEmissionError, match="ARCA rechazo"):
                await ArcaService(
                    session,
                    wsaa_factory=_FakeWsaaForEmission,
                    wsfe_factory=RejectedWsfe,
                ).emit_manual_invoice_for_patient(
                    tenant,
                    patient,
                    item,
                    amount=Decimal("1500.00"),
                    service_start=datetime(2026, 7, 1).date(),
                    service_end=datetime(2026, 7, 1).date(),
                    sale_condition="Contado",
                    send_email=False,
                )
            await session.commit()

    asyncio.run(_run())

    async def _fetch():
        async with db_session() as session:
            invoice = await session.scalar(select(ArcaInvoice).where(ArcaInvoice.tenant_id == tenant_id))
            events = list(
                (
                    await session.execute(
                        select(ArcaInvoiceEvent).where(ArcaInvoiceEvent.invoice_id == invoice.id)
                    )
                ).scalars()
            )
            return invoice, events

    invoice, events = asyncio.run(_fetch())
    assert invoice.status == ArcaInvoiceStatus.REJECTED
    rejection_events = [event for event in events if event.event_type == "authorization_rejected"]
    assert len(rejection_events) == 1
    assert rejection_events[0].payload_json["origin"] == "manual"
