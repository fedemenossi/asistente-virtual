from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.core.security import hash_password
from app.integrations.consultorio_movil import fetch_all_patients, fetch_patient_by_document
from app.models.paciente import Paciente
from app.models.user import UserRole
from app.services.patient_sync_service import (
    PatientSyncService,
    normalize_document,
    parse_consultorio_movil_patients_csv,
)
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_user, login


CSV_HEADER = (
    "Apellido,Nombres,Fecha de nacimiento,Tipo de documento,Número de documento,"
    "Financiador / Seguro,Nro. Afiliado,Email,Celular / Otro,Teléfono de casa,"
    "Género,Dirección,Número,Departamento,Piso,Localidad,Código Postal,País,Provincia\n"
)


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(CSV_HEADER + "\n".join(rows) + "\n", encoding="utf-8-sig")
    return path


def _csrf(html: str) -> str:
    return html.split('name="csrf_token" value="')[1].split('"')[0]


def test_consultorio_movil_patients_csv_parser_maps_real_columns(tmp_path):
    csv_path = _write_csv(
        tmp_path / "pacientes.csv",
        [
            "Misitti,Candela,09-11-1999,DNI,42.249.215,SWISS MEDICAL S.A.,800006 3059398020000,"
            "MISITI@example.com,011 1159658188,011 45556666,Mujer,Calle Falsa,123,A,4,CABA,1000,Argentina,Buenos Aires"
        ],
    )

    rows = parse_consultorio_movil_patients_csv(csv_path)

    assert len(rows) == 1
    row = rows[0]
    assert row.apellido == "Misitti"
    assert row.nombres == "Candela"
    assert row.fecha_nacimiento.isoformat() == "1999-11-09"
    assert row.tipo_documento == "DNI"
    assert row.document_number_normalized == "42249215"
    assert row.email == "misiti@example.com"
    assert row.celular == "0111159658188"
    assert row.financiador_seguro == "SWISS MEDICAL S.A."


def test_normalize_document_keeps_letters_for_passport():
    assert normalize_document("13028843-K") == "13028843K"
    assert normalize_document(" D 0042215 ") == "D0042215"


class FakeResponse:
    def __init__(self, text: str, *, status_code: int = 200, url: str = "https://office.consultoriomovil.net/test") -> None:
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": "text/html"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, headers=None, timeout=None):
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected url {url}")
        return self.responses[url]

    def post(self, url: str, data=None, headers=None, timeout=None):
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected url {url}")
        return self.responses[url]
        return self.responses[url]


def test_fetch_all_patients_scrapes_admin_links_detail_and_next_page():
    list_url = "https://office.consultoriomovil.net/office/patient/"
    page_2 = "https://office.consultoriomovil.net/office/patient/?page=2"
    detail_1 = "https://office.consultoriomovil.net/office/patient/1/admin"
    detail_2 = "https://office.consultoriomovil.net/office/patient/2/admin"
    session = FakeSession(
        {
            list_url: FakeResponse(
                f"""
                <html><body>
                  <a href="/office/patient/1/admin">Ver ficha administrativa</a>
                  <a rel="next" href="{page_2}">Siguiente</a>
                </body></html>
                """,
                url=list_url,
            ),
            page_2: FakeResponse(
                '<html><body><a href="/office/patient/2/admin">Ver ficha administrativa</a></body></html>',
                url=page_2,
            ),
            detail_1: FakeResponse(
                """
                <table>
                  <tr><td>Apellido</td><td>Misitti</td></tr>
                  <tr><td>Nombres</td><td>Candela</td></tr>
                  <tr><td>Fecha de nacimiento</td><td>09-11-1999</td></tr>
                  <tr><td>Tipo de documento</td><td>DNI</td></tr>
                  <tr><td>Número de documento</td><td>42249215</td></tr>
                  <tr><td>Financiador / Seguro</td><td>SWISS MEDICAL S.A.</td></tr>
                  <tr><td>Nro. Afiliado</td><td>800006</td></tr>
                  <tr><td>Email</td><td>candela@example.com</td></tr>
                  <tr><td>Celular / Otro</td><td>011 1159658188</td></tr>
                </table>
                """,
                url=detail_1,
            ),
            detail_2: FakeResponse(
                """
                <table>
                  <tr><td>Apellido</td><td>SinDoc</td></tr>
                  <tr><td>Nombres</td><td>Ana</td></tr>
                </table>
                """,
                url=detail_2,
            ),
        }
    )

    payloads = fetch_all_patients(session)

    assert len(payloads) == 2
    assert payloads[0]["Apellido"] == "Misitti"
    assert payloads[0]["Número de documento"] == "42249215"
    assert payloads[0]["_source_url"] == detail_1
    assert page_2 in session.calls


def test_fetch_patient_by_document_opens_admin_detail_from_search_candidate():
    search_url = "https://office.consultoriomovil.net/office/patient/search"
    detail_url = "https://office.consultoriomovil.net/office/patient/10011954/admin"
    session = FakeSession(
        {
            search_url: FakeResponse(
                """
                {
                  "content": [
                    {
                      "id": 10011954,
                      "name": "Misitti, Candela",
                      "document": {"type": "DNI", "number": "42249215"},
                      "email": "basico@example.com"
                    }
                  ]
                }
                """,
                url=search_url,
            ),
            detail_url: FakeResponse(
                """
                <table>
                  <tr><td>Apellido</td><td>Misitti</td></tr>
                  <tr><td>Nombres</td><td>Candela</td></tr>
                  <tr><td>Fecha de nacimiento</td><td>09-11-1999</td></tr>
                  <tr><td>Tipo de documento</td><td>DNI</td></tr>
                  <tr><td>Numero de documento</td><td>42249215</td></tr>
                  <tr><td>Financiador / Seguro</td><td>SWISS MEDICAL S.A.</td></tr>
                  <tr><td>Nro. Afiliado</td><td>800006</td></tr>
                  <tr><td>Email</td><td>detalle@example.com</td></tr>
                  <tr><td>Celular / Otro</td><td>011 1159658188</td></tr>
                  <tr><td>Telefono de casa</td><td>011 45556666</td></tr>
                  <tr><td>Genero</td><td>Mujer</td></tr>
                  <tr><td>Direccion</td><td>Calle Falsa</td></tr>
                  <tr><td>Numero</td><td>123</td></tr>
                  <tr><td>Localidad</td><td>CABA</td></tr>
                  <tr><td>Codigo Postal</td><td>1000</td></tr>
                  <tr><td>Pais</td><td>Argentina</td></tr>
                  <tr><td>Provincia</td><td>Buenos Aires</td></tr>
                </table>
                """,
                url=detail_url,
            ),
        }
    )

    payload = fetch_patient_by_document(session, "42.249.215")

    assert payload is not None
    assert payload["Email"] == "detalle@example.com"
    assert payload["Financiador / Seguro"] == "SWISS MEDICAL S.A."
    assert payload["_raw_fields"]["Direccion"] == "Calle Falsa"
    assert payload["external_patient_id"] == "10011954"
    assert detail_url in session.calls


def test_patient_sync_creates_new_patients_and_skips_missing_document(db_session, tmp_path):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync", "whatsapp:+710"))
    csv_path = _write_csv(
        tmp_path / "pacientes.csv",
        [
            "Misitti,Candela,09-11-1999,DNI,42249215,SWISS MEDICAL S.A.,800006,misitti@example.com,011 1159658188, ,Mujer,,,,,,,Argentina,",
            "SinDoc,Ana,09-11-1999,, ,OSDE,123,ana@example.com,1122334455, ,Mujer,,,,,,,Argentina,",
        ],
    )

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_from_csv(tenant_id, csv_path)

    result = asyncio.run(_run())
    assert result.total_rows == 2
    assert result.created == 1
    assert result.missing_document == 1

    async def _fetch():
        async with db_session() as session:
            return await session.scalar(
                select(Paciente).where(
                    Paciente.tenant_id == tenant_id,
                    Paciente.document_number_normalized == "42249215",
                )
            )

    paciente = asyncio.run(_fetch())
    assert paciente is not None
    assert paciente.nombre == "Candela"
    assert paciente.apellido == "Misitti"
    assert paciente.dni == "42249215"
    assert paciente.obra_social == "SWISS MEDICAL S.A."
    assert paciente.insurance_number == "800006"
    assert paciente.sync_source == "csv"
    assert paciente.raw_payload_json["Apellido"] == "Misitti"


def test_patient_sync_omits_existing_by_document_without_updating(db_session, tmp_path):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Existing", "whatsapp:+711"))
    existing_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Nombre Local",
            apellido="Apellido Local",
            dni="42249215",
            tipo_documento="DNI",
            numero_documento="42249215",
            document_number_normalized="42249215",
            email="local@example.com",
        )
    )
    csv_path = _write_csv(
        tmp_path / "pacientes.csv",
        [
            "Misitti,Candela,09-11-1999,DNI,42249215,SWISS MEDICAL S.A.,800006,nuevo@example.com,011 1159658188, ,Mujer,,,,,,,Argentina,"
        ],
    )

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_from_csv(tenant_id, csv_path)

    result = asyncio.run(_run())
    assert result.created == 0
    assert result.existing == 1

    async def _fetch():
        async with db_session() as session:
            return await session.get(Paciente, existing_id)

    paciente = asyncio.run(_fetch())
    assert paciente.nombre == "Nombre Local"
    assert paciente.email == "local@example.com"


def test_patient_sync_is_tenant_scoped(db_session, tmp_path):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Sync A", "whatsapp:+712"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Sync B", "whatsapp:+713"))
    asyncio.run(
        create_paciente(
            db_session,
            tenant_b,
            "5491111111111",
            dni="42249215",
            tipo_documento="DNI",
            numero_documento="42249215",
            document_number_normalized="42249215",
        )
    )
    csv_path = _write_csv(
        tmp_path / "pacientes.csv",
        [
            "Misitti,Candela,09-11-1999,DNI,42249215,SWISS MEDICAL S.A.,800006,misitti@example.com,011 1159658188, ,Mujer,,,,,,,Argentina,"
        ],
    )

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_from_csv(tenant_a, csv_path)

    result = asyncio.run(_run())
    assert result.created == 1
    assert result.existing == 0


def test_patient_sync_route_requires_login(client):
    response = client.post("/t/pacientes/sync-consultorio-movil", data={"csrf_token": "x"}, follow_redirects=False)
    assert response.status_code in (302, 303)


def test_patient_sync_route_scrapes_consultorio_movil_and_reports_summary(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Route", "whatsapp:+714"))
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Consultorio Movil",
            proveedor_turnos="consultorio_movil",
            configuracion_externa={"cabildo": {"user": "cm-user", "password": "cm-pass", "staff_id": "77"}},
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-sync-route@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    calls = {"login": 0, "fetch": 0}

    def fake_login(username, password):
        calls["login"] += 1
        assert (username, password) == ("cm-user", "cm-pass")
        return object()

    def fake_fetch_all_patients(session):
        calls["fetch"] += 1
        return [
            {
                "Apellido": "Misitti",
                "Nombres": "Candela",
                "Fecha de nacimiento": "09-11-1999",
                "Tipo de documento": "DNI",
                "Número de documento": "42249215",
                "Financiador / Seguro": "SWISS MEDICAL S.A.",
                "Nro. Afiliado": "800006",
                "Email": "misitti@example.com",
                "Celular / Otro": "011 1159658188",
            }
        ]

    monkeypatch.setattr("app.web.tenant.views.consultorio_movil_login", fake_login)
    monkeypatch.setattr("app.web.tenant.views.fetch_all_patients", fake_fetch_all_patients)

    login(client, "tenant-sync-route@test.com", "secret-123")
    page = client.get("/t/pacientes")
    response = client.post(
        "/t/pacientes/sync-consultorio-movil",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert calls == {"login": 1, "fetch": 1}
    assert "Sincronizacion finalizada" in response.text
    assert "Candela" in response.text


def test_patient_list_has_csv_and_consultorio_movil_actions(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient Actions", "whatsapp:+716"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-patient-actions@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-patient-actions@test.com", "secret-123")

    response = client.get("/t/pacientes")

    assert response.status_code == 200
    assert "Nuevo paciente" in response.text
    assert "Cargar con CSV" in response.text
    assert "/t/pacientes/import-csv" in response.text
    assert "Sincronizar desde Consultorio Movil" in response.text
    assert "/t/pacientes/sync-consultorio-movil" in response.text


def test_patient_csv_upload_route_imports_patients(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant CSV Upload", "whatsapp:+717"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-csv-upload@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-csv-upload@test.com", "secret-123")
    page = client.get("/t/pacientes")
    csv_content = (
        CSV_HEADER
        + "Misitti,Candela,09-11-1999,DNI,42249215,SWISS MEDICAL S.A.,800006,"
        + "misitti@example.com,011 1159658188, ,Mujer,,,,,,,Argentina,\n"
    ).encode("utf-8-sig")

    response = client.post(
        "/t/pacientes/import-csv",
        data={"csrf_token": _csrf(page.text)},
        files={"csv_file": ("pacientes.csv", csv_content, "text/csv")},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "CSV procesado" in response.text
    assert "Candela" in response.text


def test_patient_detail_sync_from_consultorio_movil_updates_existing_patient(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient One Sync", "whatsapp:+718"))
    paciente_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Nombre Local",
            apellido="Apellido Local",
            dni="42249215",
            tipo_documento="DNI",
            numero_documento="42249215",
            document_number_normalized="42249215",
            email="local@example.com",
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Consultorio Movil",
            proveedor_turnos="consultorio_movil",
            configuracion_externa={"cabildo": {"user": "cm-user", "password": "cm-pass"}},
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-one-sync@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    calls = {"login": 0, "search": 0, "fetch": 0}

    def fake_login(username, password):
        calls["login"] += 1
        assert (username, password) == ("cm-user", "cm-pass")
        return object()

    def fake_fetch_patient_by_document(session, document_number):
        calls["search"] += 1
        assert document_number == "42249215"
        return {
            "Apellido": "Misitti",
            "Nombres": "Candela",
            "Fecha de nacimiento": "09-11-1999",
            "Tipo de documento": "DNI",
            "Numero de documento": "42249215",
            "Financiador / Seguro": "SWISS MEDICAL S.A.",
            "Nro. Afiliado": "800006",
            "Email": "candela@example.com",
            "Celular / Otro": "011 1159658188",
            "external_patient_id": "cm-42249215",
        }

    def fake_fetch_all_patients(session):
        calls["fetch"] += 1
        return []

    monkeypatch.setattr("app.web.tenant.views.consultorio_movil_login", fake_login)
    monkeypatch.setattr("app.web.tenant.views.fetch_patient_by_document", fake_fetch_patient_by_document)
    monkeypatch.setattr("app.web.tenant.views.fetch_all_patients", fake_fetch_all_patients)

    login(client, "tenant-one-sync@test.com", "secret-123")
    edit = client.get(f"/t/pacientes/{paciente_id}/edit")
    assert "Sincronizar de Consultorio Movil" in edit.text

    response = client.post(
        f"/t/pacientes/{paciente_id}/sync-consultorio-movil",
        data={"csrf_token": _csrf(edit.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert calls == {"login": 1, "search": 1, "fetch": 0}

    async def _fetch():
        async with db_session() as session:
            return await session.get(Paciente, paciente_id)

    paciente = asyncio.run(_fetch())
    assert paciente.nombre == "Candela"
    assert paciente.apellido == "Misitti"
    assert paciente.email == "candela@example.com"
    assert paciente.obra_social == "SWISS MEDICAL S.A."
    assert paciente.sync_source == "consultorio_movil"


def test_patient_detail_sync_matches_by_document_when_external_type_is_missing(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient DNI Match", "whatsapp:+719"))
    paciente_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Nombre Local",
            apellido="Apellido Local",
            dni="42.249.215",
            tipo_documento="DNI",
            numero_documento="42.249.215",
            document_number_normalized="42249215",
            email="local@example.com",
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Consultorio Movil",
            proveedor_turnos="consultorio_movil",
            configuracion_externa={"cabildo": {"user": "cm-user", "password": "cm-pass"}},
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-dni-match@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    monkeypatch.setattr("app.web.tenant.views.consultorio_movil_login", lambda username, password: object())
    monkeypatch.setattr(
        "app.web.tenant.views.fetch_patient_by_document",
        lambda session, document_number: {
            "Apellido": "Misitti",
            "Nombres": "Candela",
            "Numero de documento": "42249215",
            "Email": "candela@example.com",
            "Celular / Otro": "011 1159658188",
        },
    )
    monkeypatch.setattr(
        "app.web.tenant.views.fetch_all_patients",
        lambda session: [
            {
                "Apellido": "Misitti",
                "Nombres": "Candela",
                "Numero de documento": "42249215",
                "Email": "candela@example.com",
                "Celular / Otro": "011 1159658188",
            }
        ],
    )

    login(client, "tenant-dni-match@test.com", "secret-123")
    edit = client.get(f"/t/pacientes/{paciente_id}/edit")
    response = client.post(
        f"/t/pacientes/{paciente_id}/sync-consultorio-movil",
        data={"csrf_token": _csrf(edit.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200

    async def _fetch():
        async with db_session() as session:
            return await session.get(Paciente, paciente_id)

    paciente = asyncio.run(_fetch())
    assert paciente.nombre == "Candela"
    assert paciente.document_number_normalized == "42249215"


def test_patient_form_saves_extended_fields(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient Form", "whatsapp:+715"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-patient-form@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-patient-form@test.com", "secret-123")
    form = client.get("/t/pacientes/new")
    response = client.post(
        "/t/pacientes/new",
        data={
            "csrf_token": _csrf(form.text),
            "nombre": "Candela",
            "apellido": "Misitti",
            "telefono": "011 1159658188",
            "dni": "42249215",
            "email": "candela@example.com",
            "fecha_nacimiento": "1999-11-09",
            "tipo_documento": "DNI",
            "numero_documento": "42.249.215",
            "obra_social": "SWISS MEDICAL S.A.",
            "financiador_seguro": "SWISS MEDICAL S.A.",
            "insurance_number": "800006",
            "genero": "Mujer",
            "telefono_casa": "011 45556666",
            "direccion": "Calle Falsa",
            "direccion_numero": "123",
            "departamento": "A",
            "piso": "4",
            "localidad": "CABA",
            "codigo_postal": "1000",
            "pais": "Argentina",
            "provincia": "Buenos Aires",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            return await session.scalar(
                select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.nombre == "Candela")
            )

    paciente = asyncio.run(_fetch())
    assert paciente is not None
    assert paciente.fecha_nacimiento.isoformat() == "1999-11-09"
    assert paciente.tipo_documento == "DNI"
    assert paciente.numero_documento == "42.249.215"
    assert paciente.document_number_normalized == "42249215"
    assert paciente.financiador_seguro == "SWISS MEDICAL S.A."
    assert paciente.direccion == "Calle Falsa"
