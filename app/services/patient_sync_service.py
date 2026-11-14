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
MAX_ROW_ERROR_LOGS = 10
MAX_ROW_ERROR_MESSAGES = 5
MAX_SYNC_ISSUES = 200


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
class PatientSyncIssue:
    row_index: int
    code: str
    patient_name: str
    document_type: str
    document_number: str
    message: str
    resolution: str
    related_row_index: int | None = None
    document_patient_id: int | None = None
    dni_patient_id: int | None = None


@dataclass(frozen=True)
class PatientSyncResult:
    total_rows: int
    created: int
    updated: int
    existing: int
    missing_document: int
    errors: int
    error_messages: tuple[str, ...] = ()
    conflicts: int = 0
    issues: tuple[PatientSyncIssue, ...] = ()


def normalize_document(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value or "").upper()


def normalize_document_type(value: str | None) -> str:
    return " ".join((value or "").strip().upper().split())


def normalize_phone(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def normalize_dni(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _patient_name(paciente: Paciente) -> str:
    return " ".join(part for part in (paciente.nombre, paciente.apellido) if part) or f"Paciente {paciente.id}"


def _limit(value: Any, max_length: int) -> str:
    text = " ".join(_clean(value).split())
    return text[:max_length]


def _email_value(value: Any) -> str:
    text = _clean(value).lower()
    if not text:
        return ""
    match = re.search(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", text)
    if match:
        return match.group(0)[:200]
    return _limit(text, 200)


def _safe_error_detail(exc: Exception) -> tuple[str, str]:
    original = getattr(exc, "orig", None) or exc
    error_type = type(original).__name__
    detail = " ".join(str(original).split()) or error_type
    detail = re.sub(r"(?i)(duplicate entry) '[^']*'", r"\1 '***'", detail)
    detail = re.sub(r"(?i)(value:) '[^']*'", r"\1 '***'", detail)
    detail = re.sub(r"(?i)(key \([^)]*\)=)\([^)]*\)", r"\1(***)", detail)
    return error_type, detail[:300]


def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    key = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    replacements = {
        "n_mero": "numero",
        "tel_fono": "telefono",
        "g_nero": "genero",
        "direcci_n": "direccion",
        "c_digo": "codigo",
        "pa_s": "pais",
    }
    for broken, fixed in replacements.items():
        key = key.replace(broken, fixed)
    return key


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
    tipo_documento = _limit(normalize_document_type(_raw_value(raw, "tipo de documento")), 50)
    numero_documento = _limit(_raw_value(raw, "numero de documento", "documento", "nro documento"), 64)
    return PatientSyncRow(
        apellido=_limit(_raw_value(raw, "apellido"), 100),
        nombres=_limit(_raw_value(raw, "nombres", "nombre"), 100),
        fecha_nacimiento=_parse_date(_raw_value(raw, "fecha de nacimiento", "nacimiento")),
        tipo_documento=tipo_documento,
        numero_documento=numero_documento,
        document_number_normalized=normalize_document(numero_documento),
        financiador_seguro=_limit(_raw_value(raw, "financiador seguro", "financiador", "seguro", "obra social"), 200),
        nro_afiliado=_limit(_raw_value(raw, "nro afiliado", "numero de afiliado", "afiliado"), 100),
        email=_email_value(_raw_value(raw, "email", "correo")),
        celular=normalize_phone(_raw_value(raw, "celular otro", "celular", "telefono celular"))[:32],
        telefono_casa=normalize_phone(_raw_value(raw, "telefono de casa", "telefono fijo"))[:64],
        genero=_limit(_raw_value(raw, "genero", "sexo"), 50),
        direccion=_limit(_raw_value(raw, "direccion", "domicilio"), 200),
        direccion_numero=_limit(_raw_value(raw, "numero", "altura"), 40),
        departamento=_limit(_raw_value(raw, "departamento", "depto"), 40),
        piso=_limit(_raw_value(raw, "piso"), 40),
        localidad=_limit(_raw_value(raw, "localidad"), 120),
        codigo_postal=_limit(_raw_value(raw, "codigo postal", "cp"), 20),
        pais=_limit(_raw_value(raw, "pais"), 80),
        provincia=_limit(_raw_value(raw, "provincia"), 120),
        raw_payload={str(key): value for key, value in raw.items() if key is not None},
    )


def parse_consultorio_movil_patients_csv(path: str | Path) -> list[PatientSyncRow]:
    csv_path = Path(path)
    text = csv_path.read_text(encoding="utf-8-sig")
    return parse_consultorio_movil_patients_csv_text(text)


def parse_consultorio_movil_patients_csv_text(text: str) -> list[PatientSyncRow]:
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
        update_existing: bool = False,
        progress_callback: Any | None = None,
    ) -> PatientSyncResult:
        created = 0
        updated = 0
        existing = 0
        missing_document = 0
        errors = 0
        conflicts = 0
        error_messages: list[str] = []
        issues: list[PatientSyncIssue] = []
        seen_documents: set[tuple[str, str]] = set()
        seen_dni_rows: dict[str, tuple[int, tuple[str, str]]] = {}
        synced_at = now_ba().replace(tzinfo=None)
        logger.info(
            "patient_sync_start",
            extra={"tenant_id": tenant_id, "sync_source": sync_source, "source": log_source, "rows_count": len(rows)},
        )

        for index, row in enumerate(rows, start=1):
            document_key = (row.tipo_documento, row.document_number_normalized)
            dni_normalized = normalize_dni(row.numero_documento)
            try:
                if not row.tipo_documento or not row.document_number_normalized:
                    missing_document += 1
                    if len(issues) < MAX_SYNC_ISSUES:
                        if not row.tipo_documento and not row.document_number_normalized:
                            missing_message = "Faltan el tipo y el numero de documento."
                        elif not row.tipo_documento:
                            missing_message = "Falta el tipo de documento."
                        else:
                            missing_message = "Falta el numero de documento."
                        issues.append(
                            PatientSyncIssue(
                                row_index=index,
                                code="missing_document",
                                patient_name=" ".join(part for part in (row.nombres, row.apellido) if part),
                                document_type=row.tipo_documento,
                                document_number=row.numero_documento,
                                message=missing_message,
                                resolution=(
                                    "Completa las columnas de tipo y numero de documento en el CSV y vuelve "
                                    "a importar esta fila."
                                ),
                            )
                        )
                elif document_key in seen_documents:
                    existing += 1
                elif (
                    dni_normalized
                    and dni_normalized in seen_dni_rows
                    and seen_dni_rows[dni_normalized][1] != document_key
                ):
                    related_row_index = seen_dni_rows[dni_normalized][0]
                    conflicts += 1
                    if len(issues) < MAX_SYNC_ISSUES:
                        issues.append(
                            PatientSyncIssue(
                                row_index=index,
                                related_row_index=related_row_index,
                                code="duplicate_dni_in_file",
                                patient_name=" ".join(part for part in (row.nombres, row.apellido) if part),
                                document_type=row.tipo_documento,
                                document_number=row.numero_documento,
                                message=(
                                    f"El mismo DNI aparece en las filas {related_row_index} y {index} "
                                    "con distinto tipo de documento."
                                ),
                                resolution=(
                                    "Revisa ambas filas del CSV y conserva el tipo y los datos que correspondan "
                                    "al paciente correcto."
                                ),
                            )
                        )
                else:
                    action = ""
                    async with self._session.begin_nested():
                        document_patient, dni_patient = await self._find_by_document_candidates(
                            tenant_id,
                            row.tipo_documento,
                            row.document_number_normalized,
                            dni_normalized,
                        )
                        paciente = dni_patient or document_patient
                        if (
                            document_patient is not None
                            and dni_patient is not None
                            and document_patient.id != dni_patient.id
                        ):
                            conflicts += 1
                            action = "conflict"
                            if len(issues) < MAX_SYNC_ISSUES:
                                issues.append(
                                    PatientSyncIssue(
                                        row_index=index,
                                        code="identity_conflict",
                                        patient_name=" ".join(part for part in (row.nombres, row.apellido) if part),
                                        document_type=row.tipo_documento,
                                        document_number=row.numero_documento,
                                        message=(
                                            f"El documento coincide con {_patient_name(document_patient)}, "
                                            f"pero el DNI pertenece a {_patient_name(dni_patient)}. Son dos "
                                            "pacientes diferentes del tenant."
                                        ),
                                        resolution=(
                                            "Revisa ambos pacientes y corrige o unifica sus documentos antes "
                                            "de volver a importar esta fila."
                                        ),
                                        document_patient_id=document_patient.id,
                                        dni_patient_id=dni_patient.id,
                                    )
                                )
                        elif paciente is not None:
                            if paciente.deleted_at is not None:
                                action = "existing"
                            elif update_existing:
                                self._apply_row_to_paciente(
                                    paciente,
                                    row,
                                    sync_source=sync_source,
                                    synced_at=synced_at,
                                )
                                await self._session.flush()
                                action = "updated"
                            else:
                                action = "existing"
                        else:
                            paciente = self._build_paciente(
                                tenant_id,
                                row,
                                sync_source=sync_source,
                                synced_at=synced_at,
                            )
                            self._session.add(paciente)
                            await self._session.flush()
                            action = "created"

                    if action == "created":
                        created += 1
                    elif action == "updated":
                        updated += 1
                    elif action == "existing":
                        existing += 1
                    if action != "conflict":
                        seen_documents.add(document_key)
                        if dni_normalized:
                            seen_dni_rows[dni_normalized] = (index, document_key)
            except Exception as exc:
                errors += 1
                error_type, error_detail = _safe_error_detail(exc)
                if len(issues) < MAX_SYNC_ISSUES:
                    issues.append(
                        PatientSyncIssue(
                            row_index=index,
                            code="import_error",
                            patient_name=" ".join(part for part in (row.nombres, row.apellido) if part),
                            document_type=row.tipo_documento,
                            document_number=row.numero_documento,
                            message=f"No se pudo guardar esta fila: {error_detail}",
                            resolution=(
                                "Revisa los datos de la fila y los pacientes existentes con el mismo documento. "
                                "Luego vuelve a importarla."
                            ),
                        )
                    )
                if len(error_messages) < MAX_ROW_ERROR_MESSAGES:
                    error_messages.append(f"Fila {index}: {error_type}: {error_detail}")
                if errors <= MAX_ROW_ERROR_LOGS:
                    logger.error(
                        "patient_sync_row_failed",
                        extra={
                            "tenant_id": tenant_id,
                            "row_index": index,
                            "error_type": error_type,
                            "database_error": error_detail,
                        },
                    )
                elif errors == MAX_ROW_ERROR_LOGS + 1:
                    logger.warning(
                        "patient_sync_row_errors_suppressed",
                        extra={"tenant_id": tenant_id, "logged_errors": MAX_ROW_ERROR_LOGS},
                    )
            finally:
                if progress_callback:
                    progress_callback(index, created, updated, existing, missing_document, errors)

        result = PatientSyncResult(
            total_rows=len(rows),
            created=created,
            updated=updated,
            existing=existing,
            missing_document=missing_document,
            errors=errors,
            error_messages=tuple(error_messages),
            conflicts=conflicts,
            issues=tuple(issues),
        )
        logger.info(
            "patient_sync_done",
            extra={
                "tenant_id": tenant_id,
                "sync_source": sync_source,
                "sync_total_rows": result.total_rows,
                "sync_created": result.created,
                "sync_updated": result.updated,
                "sync_existing": result.existing,
                "sync_missing_document": result.missing_document,
                "sync_conflicts": result.conflicts,
                "sync_errors": result.errors,
            },
        )
        return result

    def _build_paciente(
        self,
        tenant_id: int,
        row: PatientSyncRow,
        *,
        sync_source: str,
        synced_at: datetime,
    ) -> Paciente:
        return Paciente(
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
            external_provider=_clean(row.raw_payload.get("external_provider")) or None,
            external_patient_id=_clean(row.raw_payload.get("external_patient_id")) or None,
            sync_source=sync_source,
            synced_at=synced_at,
            external_updated_at=None,
            raw_payload_json=row.raw_payload,
        )

    def _apply_row_to_paciente(
        self,
        paciente: Paciente,
        row: PatientSyncRow,
        *,
        sync_source: str,
        synced_at: datetime,
    ) -> None:
        paciente.nombre = row.nombres or paciente.nombre
        paciente.apellido = row.apellido or paciente.apellido
        paciente.telefono = row.celular or paciente.telefono
        paciente.dni = row.numero_documento or paciente.dni
        paciente.email = row.email or paciente.email
        paciente.obra_social = row.financiador_seguro or paciente.obra_social
        paciente.insurance_number = row.nro_afiliado or paciente.insurance_number
        paciente.fecha_nacimiento = row.fecha_nacimiento or paciente.fecha_nacimiento
        paciente.tipo_documento = row.tipo_documento or paciente.tipo_documento
        paciente.numero_documento = row.numero_documento or paciente.numero_documento
        paciente.document_number_normalized = row.document_number_normalized or paciente.document_number_normalized
        paciente.financiador_seguro = row.financiador_seguro or paciente.financiador_seguro
        paciente.genero = row.genero or paciente.genero
        paciente.telefono_casa = row.telefono_casa or paciente.telefono_casa
        paciente.direccion = row.direccion or paciente.direccion
        paciente.direccion_numero = row.direccion_numero or paciente.direccion_numero
        paciente.departamento = row.departamento or paciente.departamento
        paciente.piso = row.piso or paciente.piso
        paciente.localidad = row.localidad or paciente.localidad
        paciente.codigo_postal = row.codigo_postal or paciente.codigo_postal
        paciente.pais = row.pais or paciente.pais
        paciente.provincia = row.provincia or paciente.provincia
        paciente.external_provider = _clean(row.raw_payload.get("external_provider")) or paciente.external_provider
        paciente.external_patient_id = _clean(row.raw_payload.get("external_patient_id")) or paciente.external_patient_id
        paciente.sync_source = sync_source
        paciente.synced_at = synced_at
        paciente.raw_payload_json = row.raw_payload

    async def _find_by_document(
        self,
        tenant_id: int,
        tipo_documento: str,
        document_number_normalized: str,
    ) -> Paciente | None:
        document_patient, dni_patient = await self._find_by_document_candidates(
            tenant_id,
            tipo_documento,
            document_number_normalized,
            normalize_dni(document_number_normalized),
        )
        return dni_patient or document_patient

    async def _find_by_document_candidates(
        self,
        tenant_id: int,
        tipo_documento: str,
        document_number_normalized: str,
        dni_normalized: str,
    ) -> tuple[Paciente | None, Paciente | None]:
        result = await self._session.execute(
            select(Paciente).where(
                Paciente.tenant_id == tenant_id,
                Paciente.tipo_documento == tipo_documento,
                Paciente.document_number_normalized == document_number_normalized,
            ).order_by(Paciente.deleted_at.is_not(None).asc(), Paciente.id.asc()).limit(1)
        )
        document_patient = result.scalar_one_or_none()

        dni_patient = None
        if dni_normalized:
            legacy = await self._session.execute(
                select(Paciente).where(
                    Paciente.tenant_id == tenant_id,
                    Paciente.dni_normalized == dni_normalized,
                ).order_by(Paciente.deleted_at.is_not(None).asc(), Paciente.id.asc()).limit(1)
            )
            dni_patient = legacy.scalar_one_or_none()
        return document_patient, dni_patient

    async def _exists_by_document(
        self,
        tenant_id: int,
        tipo_documento: str,
        document_number_normalized: str,
    ) -> bool:
        return await self._find_by_document(tenant_id, tipo_documento, document_number_normalized) is not None

    async def update_existing_from_payload(
        self,
        paciente: Paciente,
        payload: dict[str, Any],
        *,
        sync_source: str = "consultorio_movil",
    ) -> bool:
        row = patient_sync_row_from_payload(payload)
        if not row.document_number_normalized:
            return False
        paciente.nombre = row.nombres or paciente.nombre
        paciente.apellido = row.apellido or paciente.apellido
        paciente.telefono = row.celular or paciente.telefono
        paciente.dni = row.numero_documento or paciente.dni
        paciente.email = row.email or paciente.email
        paciente.obra_social = row.financiador_seguro or paciente.obra_social
        paciente.insurance_number = row.nro_afiliado or paciente.insurance_number
        paciente.fecha_nacimiento = row.fecha_nacimiento or paciente.fecha_nacimiento
        paciente.tipo_documento = row.tipo_documento or paciente.tipo_documento
        paciente.numero_documento = row.numero_documento or paciente.numero_documento
        paciente.document_number_normalized = row.document_number_normalized or paciente.document_number_normalized
        paciente.financiador_seguro = row.financiador_seguro or paciente.financiador_seguro
        paciente.genero = row.genero or paciente.genero
        paciente.telefono_casa = row.telefono_casa or paciente.telefono_casa
        paciente.direccion = row.direccion or paciente.direccion
        paciente.direccion_numero = row.direccion_numero or paciente.direccion_numero
        paciente.departamento = row.departamento or paciente.departamento
        paciente.piso = row.piso or paciente.piso
        paciente.localidad = row.localidad or paciente.localidad
        paciente.codigo_postal = row.codigo_postal or paciente.codigo_postal
        paciente.pais = row.pais or paciente.pais
        paciente.provincia = row.provincia or paciente.provincia
        paciente.external_provider = "consultorio_movil"
        paciente.external_patient_id = _clean(row.raw_payload.get("external_patient_id")) or paciente.external_patient_id
        paciente.sync_source = sync_source
        paciente.synced_at = now_ba().replace(tzinfo=None)
        paciente.raw_payload_json = row.raw_payload
        await self._session.flush()
        return True
