from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_ba
from app.models.paciente import Paciente

logger = logging.getLogger(__name__)

DEFAULT_CONSULTORIO_MOVIL_PATIENTS_CSV = Path("ListadoPacientes.csv")


@dataclass(frozen=True)
class PatientSyncRow:
    apellido: str
    nombres: str
    fecha_nacimiento: date | None
    tipo_documento: str
    numero_documento: str
    document_number_normalized: str
    financiador_seguro: str
    nro_afiliado: str
    email: str
    celular: str
    telefono_casa: str
    genero: str
    direccion: str
    direccion_numero: str
    departamento: str
    piso: str
    localidad: str
    codigo_postal: str
    pais: str
    provincia: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class PatientSyncResult:
    total_rows: int
    created: int
    existing: int
    missing_document: int
    errors: int


def normalize_document(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value or "").upper()


def normalize_document_type(value: str | None) -> str:
    return " ".join((value or "").strip().upper().split())


def normalize_phone(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _raw_value(raw: dict[str, Any], *names: str) -> Any:
    normalized = {_normalize_key(key): value for key, value in raw.items()}
    for name in names:
        key = _normalize_key(name)
        if key in normalized:
            return normalized[key]
    return None


def _parse_date(value: Any) -> date | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def patient_sync_row_from_payload(raw: dict[str, Any]) -> PatientSyncRow:
    tipo_documento = normalize_document_type(_raw_value(raw, "tipo de documento"))
    numero_documento = _clean(_raw_value(raw, "numero de documento", "documento", "nro documento"))
    return PatientSyncRow(
        apellido=_clean(_raw_value(raw, "apellido")),
        nombres=_clean(_raw_value(raw, "nombres", "nombre")),
        fecha_nacimiento=_parse_date(_raw_value(raw, "fecha de nacimiento", "nacimiento")),
        tipo_documento=tipo_documento,
        numero_documento=numero_documento,
        document_number_normalized=normalize_document(numero_documento),
        financiador_seguro=_clean(_raw_value(raw, "financiador seguro", "financiador", "seguro", "obra social")),
        nro_afiliado=_clean(_raw_value(raw, "nro afiliado", "numero de afiliado", "afiliado")),
        email=_clean(_raw_value(raw, "email", "correo")).lower(),
        celular=normalize_phone(_raw_value(raw, "celular otro", "celular", "telefono celular")),
        telefono_casa=normalize_phone(_raw_value(raw, "telefono de casa", "telefono fijo")),
        genero=_clean(_raw_value(raw, "genero", "sexo")),
        direccion=_clean(_raw_value(raw, "direccion", "domicilio")),
        direccion_numero=_clean(_raw_value(raw, "numero", "altura")),
        departamento=_clean(_raw_value(raw, "departamento", "depto")),
        piso=_clean(_raw_value(raw, "piso")),
        localidad=_clean(_raw_value(raw, "localidad")),
        codigo_postal=_clean(_raw_value(raw, "codigo postal", "cp")),
        pais=_clean(_raw_value(raw, "pais")),
        provincia=_clean(_raw_value(raw, "provincia")),
        raw_payload={str(key): value for key, value in raw.items() if key is not None},
    )


def parse_consultorio_movil_patients_csv(path: str | Path) -> list[PatientSyncRow]:
    csv_path = Path(path)
    text = csv_path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(text.splitlines(), delimiter=",")
    return [patient_sync_row_from_payload(raw) for raw in reader]


class PatientSyncService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sync_from_csv(
        self,
        tenant_id: int,
        path: str | Path = DEFAULT_CONSULTORIO_MOVIL_PATIENTS_CSV,
    ) -> PatientSyncResult:
        rows = parse_consultorio_movil_patients_csv(path)
        return await self.sync_rows(tenant_id, rows, sync_source="csv", log_source=str(path))

    async def sync_from_payloads(
        self,
        tenant_id: int,
        payloads: list[dict[str, Any]],
        *,
        sync_source: str = "consultorio_movil",
    ) -> PatientSyncResult:
        rows = [patient_sync_row_from_payload(payload) for payload in payloads]
        return await self.sync_rows(tenant_id, rows, sync_source=sync_source, log_source=sync_source)

    async def sync_rows(
        self,
        tenant_id: int,
        rows: list[PatientSyncRow],
        *,
        sync_source: str,
        log_source: str,
    ) -> PatientSyncResult:
        created = 0
        existing = 0
        missing_document = 0
        errors = 0
        synced_at = now_ba().replace(tzinfo=None)
        logger.info(
            "patient_sync_start",
            extra={"tenant_id": tenant_id, "sync_source": sync_source, "source": log_source, "rows_count": len(rows)},
        )

        for row in rows:
            try:
                if not row.tipo_documento or not row.document_number_normalized:
                    missing_document += 1
                    continue
                exists = await self._exists_by_document(tenant_id, row.tipo_documento, row.document_number_normalized)
                if exists:
                    existing += 1
                    continue
                paciente = Paciente(
                    tenant_id=tenant_id,
                    nombre=row.nombres,
                    apellido=row.apellido,
                    telefono=row.celular,
                    dni=row.numero_documento,
                    email=row.email,
                    obra_social=row.financiador_seguro or None,
                    insurance_number=row.nro_afiliado or None,
                    fecha_nacimiento=row.fecha_nacimiento,
                    tipo_documento=row.tipo_documento,
                    numero_documento=row.numero_documento,
                    document_number_normalized=row.document_number_normalized,
                    financiador_seguro=row.financiador_seguro or None,
                    genero=row.genero or None,
                    telefono_casa=row.telefono_casa or None,
                    direccion=row.direccion or None,
                    direccion_numero=row.direccion_numero or None,
                    departamento=row.departamento or None,
                    piso=row.piso or None,
                    localidad=row.localidad or None,
                    codigo_postal=row.codigo_postal or None,
                    pais=row.pais or None,
                    provincia=row.provincia or None,
                    external_provider="consultorio_movil",
                    external_patient_id=_clean(row.raw_payload.get("external_patient_id")) or None,
                    sync_source=sync_source,
                    synced_at=synced_at,
                    external_updated_at=None,
                    raw_payload_json=row.raw_payload,
                )
                self._session.add(paciente)
                created += 1
            except Exception:
                errors += 1
                logger.exception(
                    "patient_sync_row_failed",
                    extra={
                        "tenant_id": tenant_id,
                        "tipo_documento": row.tipo_documento,
                        "document_number_normalized": row.document_number_normalized,
                    },
                )

        await self._session.flush()
        result = PatientSyncResult(
            total_rows=len(rows),
            created=created,
            existing=existing,
            missing_document=missing_document,
            errors=errors,
        )
        logger.info(
            "patient_sync_done",
            extra={
                "tenant_id": tenant_id,
                "sync_source": sync_source,
                "sync_total_rows": result.total_rows,
                "sync_created": result.created,
                "sync_existing": result.existing,
                "sync_missing_document": result.missing_document,
                "sync_errors": result.errors,
            },
        )
        return result

    async def _exists_by_document(
        self,
        tenant_id: int,
        tipo_documento: str,
        document_number_normalized: str,
    ) -> bool:
        result = await self._session.execute(
            select(Paciente.id).where(
                Paciente.tenant_id == tenant_id,
                Paciente.deleted_at.is_(None),
                Paciente.tipo_documento == tipo_documento,
                Paciente.document_number_normalized == document_number_normalized,
            )
        )
        if result.scalar_one_or_none() is not None:
            return True

        legacy = await self._session.execute(
            select(Paciente.id).where(
                Paciente.tenant_id == tenant_id,
                Paciente.deleted_at.is_(None),
                Paciente.dni == document_number_normalized,
            )
        )
        return legacy.scalar_one_or_none() is not None
