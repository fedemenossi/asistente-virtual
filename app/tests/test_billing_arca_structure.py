from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.security import hash_password
from app.integrations.arca.wsaa_client import AccessTicket
from app.integrations.arca.wsfe_client import WsfeError, WsfeResult
from app.models.arca_billable_item import ArcaBillableItem
from app.models.arca_invoice import ArcaInvoice, ArcaInvoiceStatus
from app.models.billing_email_log import BillingEmailLog
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.billing_invoice_line import BillingInvoiceLine
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
    assert "101" in response.text
    assert "202" not in response.text

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


def test_billing_pending_imports_attended_consultations_and_skips_invoiced(
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

    listing = client.get("/t/billing/pending?date_from=2026-07-01&date_to=2026-07-02")
    assert "Juan Perez" in listing.text
    assert "juan@example.com" in listing.text
    assert "OSDE" in listing.text
    assert "Paciente Facturado" not in listing.text

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

    listing = client.get("/t/billing/pending?dni=30123456")
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


def test_arca_service_emits_invoice_with_required_diagnosis(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Emit OK", "whatsapp:+626"))
    item_id, consultation_id = asyncio.run(_create_arca_emission_seed(db_session, tenant_id))
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
    assert detail["Diagnostico"] == "Bronquitis aguda"
    assert "Bronquitis aguda" in detail["Descripcion"]
    assert captured["request"]["metadata"]["diagnosis"] == "Bronquitis aguda"

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
    assert invoice.diagnosis_original_snapshot == "Bronquitis aguda"
    assert invoice.diagnosis_final_snapshot == "Bronquitis aguda"
    assert consultation.arca_invoice_id == invoice_id
    assert consultation.status == "billed"
    assert line.diagnosis_text == "Bronquitis aguda"


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

    invoice_list = client.get("/t/billing/invoices")
    assert invoice_list.status_code == 200
    assert external_id in invoice_list.text
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


def test_billing_preview_requires_diagnosis(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Preview Diagnosis", "whatsapp:+630"))
    item_id, consultation_id = asyncio.run(
        _create_arca_emission_seed(db_session, tenant_id, diagnosis=None)
    )
    del item_id
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
    assert response.status_code in (302, 303)

    async def _count_invoices():
        async with db_session() as session:
            return await session.scalar(
                select(ArcaInvoice).where(ArcaInvoice.tenant_id == tenant_id)
            )

    assert asyncio.run(_count_invoices()) is None


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
            return await BillingInvoiceDocumentService(session).build_document(tenant, invoice)

    document = asyncio.run(_build())
    assert "Diagnostico visible obligatorio" in document.html
    assert document.pdf.startswith(b"%PDF")
    assert b"Diagnostico" in document.pdf
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
    assert "Laringitis aguda" in sent["body"]
    assert "Laringitis aguda" in sent["html_body"]
    assert sent["attachments"][0][0].endswith(".pdf")
    assert sent["attachments"][0][1].startswith(b"%PDF")

    async def _fetch_log():
        async with db_session() as session:
            return await session.get(BillingEmailLog, log_id)

    log = asyncio.run(_fetch_log())
    assert log.status == "sent"
    assert log.recipient_email == "paciente@example.com"
    assert log.sent_at is not None


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
