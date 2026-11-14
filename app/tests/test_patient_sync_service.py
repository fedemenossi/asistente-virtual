from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.core.security import hash_password
from app.integrations.consultorio_movil import fetch_all_patients, fetch_patient_by_document, login as cm_login
from app.models.paciente import Paciente
from app.models.user import UserRole
from app.services.patient_sync_service import (
    PatientSyncService,
    normalize_document,
    parse_consultorio_movil_patients_csv,
    patient_sync_row_from_payload,
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


def test_patient_sync_error_detail_redacts_document_values():
    from app.services.patient_sync_service import _safe_error_detail

    _, mysql_detail = _safe_error_detail(
        ValueError("Duplicate entry '28077008' for key 'uq_paciente_tenant_dni'")
    )
    _, postgres_detail = _safe_error_detail(
        ValueError("Key (tenant_id, dni_normalized)=(1, 28077008) already exists")
    )

    assert "28077008" not in mysql_detail
    assert "28077008" not in postgres_detail


def test_patient_payload_limits_long_email_field():
    from app.services.patient_sync_service import patient_sync_row_from_payload

    row = patient_sync_row_from_payload(
        {
            "Apellido": "Misitti",
            "Nombres": "Candela",
            "Tipo de documento": "DNI",
            "Numero de documento": "42249215",
            "Email": "candela@example.com " + ("texto largo " * 80),
            "Financiador / Seguro": "OSDE " * 80,
        }
    )

    assert row.email == "candela@example.com"
    assert len(row.financiador_seguro) == 200


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


class SequentialLoginSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.get_calls = 0
        self.post_calls = 0

    def get(self, url: str, timeout=None, headers=None):
        self.get_calls += 1
        if self.get_calls == 1:
            return FakeResponse("", status_code=403, url=url)
        return FakeResponse("", status_code=200, url=url)

    def post(self, url: str, data=None, headers=None, timeout=None):
        self.post_calls += 1
        return FakeResponse('{"success": true}', status_code=200, url=url)


def test_consultorio_movil_login_retries_initial_403(monkeypatch):
    fake_session = SequentialLoginSession()
    monkeypatch.setattr("app.integrations.consultorio_movil.requests.Session", lambda: fake_session)
    monkeypatch.setattr("app.integrations.consultorio_movil.time.sleep", lambda seconds: None)

    session = cm_login("user@example.com", "secret")

    assert session is fake_session
    assert fake_session.get_calls == 2
    assert fake_session.post_calls == 1


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
    assert result.issues[0].row_index == 2
    assert result.issues[0].code == "missing_document"
    assert result.issues[0].patient_name == "Ana SinDoc"

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



def test_patient_list_has_new_and_csv_actions_only(client, db_session):
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
    assert "data-patient-csv-progress" in response.text
    assert "data-patient-csv-input" in response.text
    assert "Sincronizar desde Consultorio Movil" not in response.text
    assert "/t/pacientes/sync-consultorio-movil" not in response.text


def test_patient_csv_upload_route_starts_background_job(client, db_session):
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
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert "/t/settings/patients?job_id=" in response.headers["location"]


def test_patient_settings_page_shows_import_controls(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient Settings", "whatsapp:+720"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-patient-settings@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-patient-settings@test.com", "secret-123")

    response = client.get("/t/settings/patients")

    assert response.status_code == 200
    assert "Datos de pacientes" in response.text
    assert "Importacion CSV" in response.text
    assert "data-patient-csv-progress" in response.text


def test_patient_sync_upsert_updates_existing_by_document(db_session, tmp_path):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant CSV Upsert", "whatsapp:+721"))
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
            "Misitti,Candela,09-11-1999,DNI,42249215,SWISS MEDICAL S.A.,800006,nuevo@example.com,011 1159658188, ,Mujer,Calle Falsa,123,A,4,CABA,1000,Argentina,Buenos Aires"
        ],
    )

    async def _run():
        async with db_session() as session:
            async with session.begin():
                rows = parse_consultorio_movil_patients_csv(csv_path)
                return await PatientSyncService(session).sync_rows(
                    tenant_id,
                    rows,
                    sync_source="csv",
                    log_source=str(csv_path),
                    update_existing=True,
                )

    result = asyncio.run(_run())
    assert result.created == 0
    assert result.updated == 1

    async def _fetch():
        async with db_session() as session:
            return await session.get(Paciente, existing_id)

    paciente = asyncio.run(_fetch())
    assert paciente.nombre == "Candela"
    assert paciente.apellido == "Misitti"
    assert paciente.email == "nuevo@example.com"
    assert paciente.direccion == "Calle Falsa"


def test_patient_sync_rolls_back_only_the_invalid_row_and_continues(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Savepoint", "whatsapp:+722"))
    rows = [
        patient_sync_row_from_payload(
            {
                "Apellido": "Invalido",
                "Nombres": "Paciente",
                "Tipo de documento": "DNI",
                "Numero de documento": "11111111",
            }
        ),
        patient_sync_row_from_payload(
            {
                "Apellido": "Valido",
                "Nombres": "Paciente",
                "Tipo de documento": "DNI",
                "Numero de documento": "22222222",
            }
        ),
    ]
    original_build = PatientSyncService._build_paciente

    def _build_with_invalid_name(self, tenant_id, row, *, sync_source, synced_at):
        paciente = original_build(
            self,
            tenant_id,
            row,
            sync_source=sync_source,
            synced_at=synced_at,
        )
        if row.document_number_normalized == "11111111":
            paciente.nombre = None
        return paciente

    monkeypatch.setattr(PatientSyncService, "_build_paciente", _build_with_invalid_name)

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_rows(
                    tenant_id,
                    rows,
                    sync_source="csv",
                    log_source="pacientes.csv",
                )

    result = asyncio.run(_run())

    assert result.created == 1
    assert result.errors == 1
    assert result.error_messages[0].startswith("Fila 1:")
    assert result.issues[0].code == "import_error"
    assert result.issues[0].row_index == 1
    assert result.issues[0].patient_name == "Paciente Invalido"
    assert "No se pudo guardar" in result.issues[0].message

    async def _documents():
        async with db_session() as session:
            values = await session.scalars(
                select(Paciente.document_number_normalized).where(Paciente.tenant_id == tenant_id)
            )
            return list(values)

    assert asyncio.run(_documents()) == ["22222222"]


def test_patient_sync_reports_an_identity_conflict_and_continues(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Identity Conflict", "whatsapp:+728"))
    document_patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111728",
            nombre="Candela",
            apellido="Misitti",
            dni="11111111",
            tipo_documento="DNI",
            numero_documento="42249215",
            document_number_normalized="42249215",
        )
    )
    dni_patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111729",
            nombre="Otro",
            apellido="Paciente",
            dni="42249215",
            tipo_documento="DNI",
            numero_documento="99999999",
            document_number_normalized="99999999",
        )
    )
    rows = [
        patient_sync_row_from_payload(
            {
                "Apellido": "Misitti",
                "Nombres": "Candela",
                "Tipo de documento": "DNI",
                "Numero de documento": "42249215",
            }
        ),
        patient_sync_row_from_payload(
            {
                "Apellido": "Valido",
                "Nombres": "Paciente",
                "Tipo de documento": "DNI",
                "Numero de documento": "55555555",
            }
        ),
    ]

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_rows(
                    tenant_id,
                    rows,
                    sync_source="csv",
                    log_source="pacientes.csv",
                    update_existing=True,
                )

    result = asyncio.run(_run())

    assert result.created == 1
    assert result.updated == 0
    assert result.errors == 0
    assert result.conflicts == 1
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.row_index == 1
    assert issue.code == "identity_conflict"
    assert issue.patient_name == "Candela Misitti"
    assert issue.document_number == "42249215"
    assert issue.document_patient_id == document_patient_id
    assert issue.dni_patient_id == dni_patient_id
    assert "dos pacientes diferentes" in issue.message


def test_patient_sync_reports_same_dni_on_different_csv_rows(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync CSV Conflict", "whatsapp:+729"))
    rows = [
        patient_sync_row_from_payload(
            {
                "Apellido": "Kirillova",
                "Nombres": "Milena",
                "Tipo de documento": "CI",
                "Numero de documento": "736340920",
            }
        ),
        patient_sync_row_from_payload(
            {
                "Apellido": "Kirillova",
                "Nombres": "Nika",
                "Tipo de documento": "CEDULA DE EXTRANJERIA",
                "Numero de documento": "736340920",
            }
        ),
    ]

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_rows(
                    tenant_id,
                    rows,
                    sync_source="csv",
                    log_source="pacientes.csv",
                    update_existing=True,
                )

    result = asyncio.run(_run())

    assert result.created == 1
    assert result.updated == 0
    assert result.errors == 0
    assert result.conflicts == 1
    issue = result.issues[0]
    assert issue.row_index == 2
    assert issue.related_row_index == 1
    assert issue.code == "duplicate_dni_in_file"
    assert "filas 1 y 2" in issue.message


def test_patient_sync_counts_duplicate_rows_as_existing(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Duplicates", "whatsapp:+723"))
    row = patient_sync_row_from_payload(
        {
            "Apellido": "Duplicado",
            "Nombres": "Paciente",
            "Tipo de documento": "DNI",
            "Numero de documento": "33333333",
        }
    )

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_rows(
                    tenant_id,
                    [row, row],
                    sync_source="csv",
                    log_source="pacientes.csv",
                    update_existing=True,
                )

    result = asyncio.run(_run())

    assert result.created == 1
    assert result.updated == 0
    assert result.existing == 1
    assert result.errors == 0


def test_patient_sync_does_not_recreate_a_soft_deleted_patient(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Deleted", "whatsapp:+724"))
    paciente_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111124",
            dni="44444444",
            tipo_documento="DNI",
            numero_documento="44444444",
            document_number_normalized="44444444",
        )
    )

    async def _delete():
        async with db_session() as session:
            async with session.begin():
                paciente = await session.get(Paciente, paciente_id)
                paciente.deleted_at = datetime(2026, 11, 13, 18, 0, 0)

    asyncio.run(_delete())
    row = patient_sync_row_from_payload(
        {
            "Apellido": "Eliminado",
            "Nombres": "Paciente",
            "Tipo de documento": "DNI",
            "Numero de documento": "44444444",
        }
    )

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_rows(
                    tenant_id,
                    [row],
                    sync_source="csv",
                    log_source="pacientes.csv",
                    update_existing=True,
                )

    result = asyncio.run(_run())

    assert result.created == 0
    assert result.updated == 0
    assert result.existing == 1
    assert result.errors == 0


def test_patient_sync_limits_row_error_logs(db_session, monkeypatch, caplog):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Log Limit", "whatsapp:+725"))
    rows = [
        patient_sync_row_from_payload(
            {
                "Apellido": "Invalido",
                "Nombres": "Paciente",
                "Tipo de documento": "DNI",
                "Numero de documento": str(50000000 + index),
            }
        )
        for index in range(12)
    ]
    original_build = PatientSyncService._build_paciente

    def _always_invalid(self, tenant_id, row, *, sync_source, synced_at):
        paciente = original_build(
            self,
            tenant_id,
            row,
            sync_source=sync_source,
            synced_at=synced_at,
        )
        paciente.nombre = None
        return paciente

    monkeypatch.setattr(PatientSyncService, "_build_paciente", _always_invalid)

    async def _run():
        async with db_session() as session:
            async with session.begin():
                return await PatientSyncService(session).sync_rows(
                    tenant_id,
                    rows,
                    sync_source="csv",
                    log_source="pacientes.csv",
                )

    result = asyncio.run(_run())

    messages = [record.message for record in caplog.records]
    assert result.errors == 12
    assert len(result.error_messages) == 5
    assert messages.count("patient_sync_row_failed") == 10
    assert messages.count("patient_sync_row_errors_suppressed") == 1


def test_patient_import_job_exposes_a_compact_row_error_summary(db_session, monkeypatch):
    from app.services import patient_import_job_service as job_service
    from app.services.patient_sync_service import PatientSyncResult

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Import Summary", "whatsapp:+726"))
    job = job_service.PatientImportJob(
        id="job-with-row-errors",
        tenant_id=tenant_id,
        filename="pacientes.csv",
        total_rows=3,
    )
    job_service._jobs[job.id] = job

    async def _sync_rows(*args, **kwargs):
        return PatientSyncResult(
            total_rows=3,
            created=2,
            updated=0,
            existing=0,
            missing_document=0,
            errors=1,
            error_messages=("Fila 2: IntegrityError: dato invalido",),
        )

    monkeypatch.setattr(job_service, "AsyncSessionLocal", db_session)
    monkeypatch.setattr(PatientSyncService, "sync_rows", _sync_rows)

    asyncio.run(job_service._run_patient_csv_import_job(job.id, []))

    assert job.status == "completed_with_errors"
    assert job.error_message == "1 fila no pudo importarse. Fila 2: IntegrityError: dato invalido"


def test_patient_import_job_exposes_structured_issues(db_session, monkeypatch):
    from app.services import patient_import_job_service as job_service
    from app.services.patient_sync_service import PatientSyncIssue, PatientSyncResult

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Import Issues", "whatsapp:+730"))
    job = job_service.PatientImportJob(
        id="job-with-structured-issues",
        tenant_id=tenant_id,
        filename="pacientes.csv",
        total_rows=2,
    )
    job_service._jobs[job.id] = job
    issue = PatientSyncIssue(
        row_index=1,
        code="identity_conflict",
        patient_name="Candela Misitti",
        document_type="DNI",
        document_number="42249215",
        message="El documento y el DNI coinciden con dos pacientes diferentes del tenant.",
        resolution="Revisa ambos pacientes.",
        document_patient_id=10,
        dni_patient_id=11,
    )

    async def _sync_rows(*args, **kwargs):
        return PatientSyncResult(
            total_rows=2,
            created=1,
            updated=0,
            existing=0,
            missing_document=0,
            errors=0,
            conflicts=1,
            issues=(issue,),
        )

    monkeypatch.setattr(job_service, "AsyncSessionLocal", db_session)
    monkeypatch.setattr(PatientSyncService, "sync_rows", _sync_rows)

    asyncio.run(job_service._run_patient_csv_import_job(job.id, []))

    assert job.status == "completed_with_issues"
    assert job.conflicts == 1
    assert job.error_message == "1 fila requiere revision. Las demas filas se importaron correctamente."
    assert job.public_dict()["issues"] == [
        {
            "row_index": 1,
            "code": "identity_conflict",
            "patient_name": "Candela Misitti",
            "document_type": "DNI",
            "document_number": "42249215",
            "message": "El documento y el DNI coinciden con dos pacientes diferentes del tenant.",
            "resolution": "Revisa ambos pacientes.",
            "related_row_index": None,
            "document_patient_id": 10,
            "dni_patient_id": 11,
        }
    ]


def test_patient_settings_shows_a_completed_import_error_summary(client, db_session):
    from app.services import patient_import_job_service as job_service

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Import Error UI", "whatsapp:+727"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-import-error-ui@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    job = job_service.PatientImportJob(
        id="completed-job-with-errors",
        tenant_id=tenant_id,
        filename="pacientes.csv",
        total_rows=3,
        status="completed_with_errors",
        processed=3,
        created=2,
        errors=1,
        error_message="1 fila no pudo importarse. Fila 2: dato invalido",
    )
    job_service._jobs[job.id] = job
    job_service._tenant_latest[tenant_id] = job.id
    login(client, "tenant-import-error-ui@test.com", "secret-123")

    response = client.get("/t/settings/patients")

    assert response.status_code == 200
    assert 'class="rounded-lg bg-rose-50' in response.text
    assert "1 fila no pudo importarse. Fila 2: dato invalido" in response.text


def test_patient_settings_shows_actionable_import_issues(client, db_session):
    from app.services import patient_import_job_service as job_service
    from app.services.patient_sync_service import PatientSyncIssue

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Import Issues UI", "whatsapp:+731"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-import-issues-ui@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    issue = PatientSyncIssue(
        row_index=129,
        code="identity_conflict",
        patient_name="Lucia Paula Conte",
        document_type="DNI",
        document_number="27087640",
        message="El documento y el DNI coinciden con dos pacientes diferentes del tenant.",
        resolution="Revisa ambos pacientes y corrige sus documentos.",
        document_patient_id=21,
        dni_patient_id=22,
    )
    job = job_service.PatientImportJob(
        id="completed-job-with-actionable-issues",
        tenant_id=tenant_id,
        filename="pacientes.csv",
        total_rows=130,
        status="completed_with_issues",
        processed=130,
        created=129,
        conflicts=1,
        issues=[issue],
        error_message="1 fila requiere revision. Las demas filas se importaron correctamente.",
    )
    job_service._jobs[job.id] = job
    job_service._tenant_latest[tenant_id] = job.id
    login(client, "tenant-import-issues-ui@test.com", "secret-123")

    response = client.get("/t/settings/patients")

    assert response.status_code == 200
    assert "Filas que requieren revision" in response.text
    assert "Fila 129" in response.text
    assert "Lucia Paula Conte" in response.text
    assert "DNI 27087640" in response.text
    assert "Revisa ambos pacientes y corrige sus documentos." in response.text
    assert 'href="/t/pacientes/21/edit"' in response.text
    assert 'href="/t/pacientes/22/edit"' in response.text

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
