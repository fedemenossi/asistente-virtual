from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from app.core.security import hash_password
from app.models.paciente import Paciente
from app.models.user import UserRole
from app.services.patient_sync_service import (
    PatientSyncService,
    normalize_document,
    parse_consultorio_movil_patients_csv,
)
from app.tests.conftest import create_paciente, create_tenant, create_user, login


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


def test_patient_sync_route_imports_csv_and_reports_summary(client, db_session, tmp_path, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync Route", "whatsapp:+714"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-sync-route@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    csv_path = _write_csv(
        tmp_path / "pacientes.csv",
        [
            "Misitti,Candela,09-11-1999,DNI,42249215,SWISS MEDICAL S.A.,800006,misitti@example.com,011 1159658188, ,Mujer,,,,,,,Argentina,"
        ],
    )
    monkeypatch.setattr("app.web.tenant.views.DEFAULT_CONSULTORIO_MOVIL_PATIENTS_CSV", csv_path)

    login(client, "tenant-sync-route@test.com", "secret-123")
    page = client.get("/t/pacientes")
    response = client.post(
        "/t/pacientes/sync-consultorio-movil",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Sincronizacion finalizada" in response.text
    assert "Candela" in response.text


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
