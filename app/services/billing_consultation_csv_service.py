from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.arca_billable_item import ArcaBillableItem
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.paciente import Paciente
from app.core.timezone import get_ba_tz


@dataclass(frozen=True)
class BillingCsvImportResult:
    total_rows: int
    created: int
    updated: int
    matched_patients: int
    missing_patient_match: int
    skipped_billed: int
    errors: int


def parse_billing_attended_csv_text(text: str) -> list[dict[str, Any]]:
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return [{str(key or "").strip(): value for key, value in raw.items()} for raw in reader]


class BillingConsultationCsvImportService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def import_rows(
        self,
        tenant_id: int,
        rows: list[dict[str, Any]],
        *,
        filename: str,
        batch_id: str,
    ) -> BillingCsvImportResult:
        default_item = await self._default_item(tenant_id)
        patients = await self._patients_by_name(tenant_id)
        created = 0
        updated = 0
        matched = 0
        missing_match = 0
        skipped_billed = 0
        errors = 0

        for index, raw in enumerate(rows, start=1):
            try:
                normalized = _normalize_payload(raw)
                name = normalized["patient_name"]
                external_patient_id = normalized["patient_external_id"]
                attended_at = _parse_date(normalized["attended_at"])
                patient = patients.get(_name_key(name))
                if patient is None:
                    patient = _find_patient_by_loose_name(patients, name)
                external_id = _external_id(filename, index, raw)
                existing = await self._session.scalar(
                    select(BillingExternalConsultation).where(
                        BillingExternalConsultation.tenant_id == tenant_id,
                        BillingExternalConsultation.external_provider == "csv_attended",
                        BillingExternalConsultation.external_id == external_id,
                    )
                )
                if existing is None:
                    existing = await self._session.scalar(
                        select(BillingExternalConsultation).where(
                            BillingExternalConsultation.tenant_id == tenant_id,
                            BillingExternalConsultation.external_id == external_id,
                        )
                    )
                row = existing or BillingExternalConsultation(
                    tenant_id=tenant_id,
                    external_provider="csv_attended",
                    external_id=external_id,
                )
                row.import_batch_id = batch_id
                row.attended_at = attended_at or row.attended_at
                row.patient_name = name or row.patient_name or None
                row.patient_external_id = external_patient_id or row.patient_external_id or None
                row.patient_id = patient.id if patient else row.patient_id
                row.patient_document = _patient_document(patient) if patient else row.patient_document
                row.patient_email = normalized["patient_email"] or (patient.email if patient else None) or row.patient_email
                row.patient_phone = normalized["patient_phone"] or (patient.telefono if patient else None) or row.patient_phone
                row.insurance_name = normalized["insurance_name"] or (patient.obra_social if patient else None) or row.insurance_name
                row.professional_name = normalized["professional_name"] or row.professional_name or None
                row.practice_name = normalized["practice_name"] or row.practice_name or None
                row.diagnosis_original = row.diagnosis_original or normalized["diagnosis"] or None
                row.diagnosis = row.diagnosis or normalized["diagnosis"] or None
                row.billing_item_id = row.billing_item_id or (default_item.id if default_item else None)
                row.amount = row.amount or (Decimal(str(default_item.unit_price)) if default_item else None)
                row.send_email = bool(row.patient_email) if row.send_email is False else row.send_email
                if existing and existing.arca_invoice_id is not None:
                    row.status = "billed"
                    skipped_billed += 1
                else:
                    row.status = "pending" if patient else "missing_patient_match"
                row.raw_payload_json = {"source_filename": filename, "import_batch_id": batch_id, "row_number": index, "row": raw}
                if patient:
                    matched += 1
                else:
                    missing_match += 1
                if existing is None:
                    self._session.add(row)
                    created += 1
                else:
                    updated += 1
            except Exception:
                errors += 1

        await self._session.flush()
        return BillingCsvImportResult(
            total_rows=len(rows),
            created=created,
            updated=updated,
            matched_patients=matched,
            missing_patient_match=missing_match,
            skipped_billed=skipped_billed,
            errors=errors,
        )

    async def _default_item(self, tenant_id: int) -> ArcaBillableItem | None:
        item = await self._session.scalar(
            select(ArcaBillableItem).where(
                ArcaBillableItem.tenant_id == tenant_id,
                ArcaBillableItem.active.is_(True),
                ArcaBillableItem.default_item.is_(True),
            )
        )
        if item is not None:
            return item
        return await self._session.scalar(
            select(ArcaBillableItem)
            .where(ArcaBillableItem.tenant_id == tenant_id, ArcaBillableItem.active.is_(True))
            .order_by(ArcaBillableItem.id.asc())
        )

    async def _patients_by_name(self, tenant_id: int) -> dict[str, Paciente]:
        result = await self._session.execute(
            select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.deleted_at.is_(None))
        )
        patients: dict[str, Paciente] = {}
        for patient in result.scalars().all():
            keys = {
                _name_key(f"{patient.apellido or ''} {patient.nombre or ''}"),
                _name_key(f"{patient.nombre or ''} {patient.apellido or ''}"),
            }
            for key in keys:
                if key and key not in patients:
                    patients[key] = patient
        return patients


def _normalize_payload(raw: dict[str, Any]) -> dict[str, str]:
    raw_patient = _value(raw, "paciente", "nombre paciente", "apellido y nombre", "nombre", "patient")
    patient_name, patient_external_id = _split_patient_name_and_external_id(raw_patient)
    return {
        "attended_at": _value(raw, "fecha", "fecha de atencion", "fecha atencion", "atendido", "dia"),
        "patient_name": patient_name,
        "patient_external_id": patient_external_id,
        "patient_email": _value(raw, "email", "mail", "correo"),
        "patient_phone": _value(raw, "telefono", "celular", "telefono paciente"),
        "insurance_name": _value(raw, "obra social", "financiador", "seguro", "cobertura"),
        "professional_name": _value(raw, "profesional", "medico", "médico", "staff"),
        "practice_name": _value(raw, "practica", "prestacion", "consulta", "servicio"),
        "diagnosis": _value(raw, "diagnostico", "diagnosis", "observaciones"),
    }


def _split_patient_name_and_external_id(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    match = re.search(r"\(([^()]*)\)\s*$", text)
    if not match:
        return text[:200], ""
    name = text[: match.start()].strip()
    external_id = re.sub(r"\s+", "", match.group(1).strip())
    return name[:200], external_id[:120]


def _value(raw: dict[str, Any], *names: str) -> str:
    normalized = {_key(key): str(value or "").strip() for key, value in raw.items()}
    for name in names:
        value = normalized.get(_key(name))
        if value:
            return value[:500]
    return ""


def _key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _name_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(tokens)


def _find_patient_by_loose_name(patients: dict[str, Paciente], value: str) -> Paciente | None:
    key = _name_key(value)
    if not key:
        return None
    value_tokens = set(key.split())
    matches = [patient for patient_key, patient in patients.items() if value_tokens and value_tokens.issubset(set(patient_key.split()))]
    if len({patient.id for patient in matches}) == 1:
        return matches[0]
    return None


def _patient_document(patient: Paciente | None) -> str | None:
    if patient is None:
        return None
    return patient.numero_documento or patient.dni or patient.document_number_normalized


def _parse_date(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in (
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=get_ba_tz()).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _external_id(filename: str, index: int, raw: dict[str, Any]) -> str:
    explicit = _value(raw, "id", "id consulta", "turno", "codigo")
    if explicit:
        return explicit[:120]
    digest = hashlib.sha1(f"{filename}:{index}:{raw}".encode("utf-8", errors="ignore")).hexdigest()
    return f"{filename}:{index}:{digest}"[:120]
