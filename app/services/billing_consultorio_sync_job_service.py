from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_ba
from app.integrations.consultorio_movil import AttendedConsultation, fetch_attended_consultations, login
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.billing_sync_state import BillingSyncState
from app.models.consultorio import Consultorio
from app.services.billing_consultation_csv_service import BillingConsultationCsvImportService

logger = logging.getLogger(__name__)


@dataclass
class BillingConsultorioSyncJob:
    id: str
    tenant_id: int
    consultorio_id: int | None
    status: str = "queued"
    phase: str = "En cola"
    total_rows: int = 0
    processed: int = 0
    created: int = 0
    updated: int = 0
    matched_patients: int = 0
    missing_patient_match: int = 0
    skipped_billed: int = 0
    errors: int = 0
    error_message: str = ""
    date_from: str = ""
    date_to: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: now_ba().replace(tzinfo=None))

    @property
    def percent(self) -> int:
        if self.status in {"completed", "completed_with_errors", "failed"}:
            return 100
        if self.total_rows <= 0:
            return 15 if self.status == "running" else 0
        return min(95, int((self.processed / self.total_rows) * 100))

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "consultorio_id": self.consultorio_id,
            "status": self.status,
            "phase": self.phase,
            "total_rows": self.total_rows,
            "processed": self.processed,
            "percent": self.percent,
            "created": self.created,
            "updated": self.updated,
            "matched_patients": self.matched_patients,
            "missing_patient_match": self.missing_patient_match,
            "skipped_billed": self.skipped_billed,
            "errors": self.errors,
            "error_message": self.error_message,
            "date_from": self.date_from,
            "date_to": self.date_to,
        }


_jobs: dict[str, BillingConsultorioSyncJob] = {}
_tenant_latest: dict[int, str] = {}


def start_billing_consultorio_sync_job(tenant_id: int, consultorio_id: int | None = None) -> BillingConsultorioSyncJob:
    job = BillingConsultorioSyncJob(id=uuid.uuid4().hex, tenant_id=tenant_id, consultorio_id=consultorio_id)
    _jobs[job.id] = job
    _tenant_latest[tenant_id] = job.id
    asyncio.create_task(_run_billing_consultorio_sync_job(job.id))
    return job


def get_billing_consultorio_sync_job(job_id: str, tenant_id: int) -> BillingConsultorioSyncJob | None:
    job = _jobs.get(job_id)
    if job is None or job.tenant_id != tenant_id:
        return None
    return job


def get_latest_billing_consultorio_sync_job(tenant_id: int) -> BillingConsultorioSyncJob | None:
    job_id = _tenant_latest.get(tenant_id)
    return get_billing_consultorio_sync_job(job_id, tenant_id) if job_id else None


async def _run_billing_consultorio_sync_job(job_id: str) -> None:
    job = _jobs[job_id]
    job.status = "running"
    job.started_at = now_ba().replace(tzinfo=None)
    try:
        async with AsyncSessionLocal() as session:
            job.phase = "Buscando consultorio configurado"
            consultorio = await _sync_consultorio(session, job.tenant_id, job.consultorio_id)
            if consultorio is None:
                raise RuntimeError("No hay consultorio con proveedor Consultorio Movil configurado.")
            job.consultorio_id = consultorio.id
            cfg = ((consultorio.configuracion_externa or {}).get("cabildo") or {})
            username = str(cfg.get("user") or "").strip()
            password = str(cfg.get("password") or "").strip()
            staff_id = str(cfg.get("staff_id") or "").strip()
            if not username or not password or not staff_id:
                raise RuntimeError("Faltan credenciales o staff_id de Consultorio Movil en el consultorio.")

            state = await _sync_state(session, job.tenant_id, consultorio.id)
            latest_attended_at = await _latest_imported_attended_at(session, job.tenant_id, consultorio.id)
            date_from = (latest_attended_at.date() - timedelta(days=1)) if latest_attended_at else date(2026, 1, 1)
            date_to = now_ba().date()
            job.date_from = date_from.isoformat()
            job.date_to = date_to.isoformat()

            job.phase = "Iniciando sesion"
            external_session = await asyncio.to_thread(login, username, password)
            job.phase = "Leyendo pacientes atendidos"
            consultations = await asyncio.to_thread(fetch_attended_consultations, external_session, staff_id, date_from, date_to)
            rows = [_row_from_attended(item) for item in consultations]
            job.total_rows = len(rows)
            job.phase = "Importando consultas"
            result = await BillingConsultationCsvImportService(session).import_rows(
                job.tenant_id,
                rows,
                filename="consultorio_movil_sync",
                batch_id=f"cm-sync-{job.id}",
                external_provider="consultorio_movil_sync",
                consultorio_id=consultorio.id,
            )
            job.processed = result.total_rows
            job.created = result.created
            job.updated = result.updated
            job.matched_patients = result.matched_patients
            job.missing_patient_match = result.missing_patient_match
            job.skipped_billed = result.skipped_billed
            job.errors = result.errors
            state.last_status = "completed" if result.errors == 0 else "completed_with_errors"
            state.last_error = None if result.errors == 0 else f"Errores de importacion: {result.errors}"
            state.last_result = json.dumps(result.__dict__, ensure_ascii=False)
            state.last_synced_at = now_ba().replace(tzinfo=None)
            await session.commit()
        job.status = "completed" if job.errors == 0 else "completed_with_errors"
        job.phase = "Finalizada"
    except Exception as exc:
        logger.warning("billing_consultorio_sync_job_failed", extra={"job_id": job.id, "tenant_id": job.tenant_id}, exc_info=True)
        job.status = "failed"
        job.phase = "Error"
        job.error_message = str(exc) or exc.__class__.__name__
        async with AsyncSessionLocal() as session:
            if job.consultorio_id is not None:
                state = await _sync_state(session, job.tenant_id, job.consultorio_id)
                state.last_status = "failed"
                state.last_error = job.error_message
                await session.commit()
    finally:
        job.finished_at = now_ba().replace(tzinfo=None)


async def _sync_consultorio(session, tenant_id: int, consultorio_id: int | None) -> Consultorio | None:
    stmt = select(Consultorio).where(Consultorio.tenant_id == tenant_id, Consultorio.deleted_at.is_(None))
    if consultorio_id:
        stmt = stmt.where(Consultorio.id == consultorio_id)
    else:
        stmt = stmt.where(Consultorio.proveedor_turnos == "consultorio_movil")
    return await session.scalar(stmt.order_by(Consultorio.id.asc()))


async def _sync_state(session, tenant_id: int, consultorio_id: int) -> BillingSyncState:
    state = await session.scalar(
        select(BillingSyncState).where(
            BillingSyncState.tenant_id == tenant_id,
            BillingSyncState.consultorio_id == consultorio_id,
            BillingSyncState.source == "consultorio_movil",
        )
    )
    if state is None:
        state = BillingSyncState(tenant_id=tenant_id, consultorio_id=consultorio_id, source="consultorio_movil")
        session.add(state)
        await session.flush()
    return state


async def _latest_imported_attended_at(session, tenant_id: int, consultorio_id: int) -> datetime | None:
    return await session.scalar(
        select(func.max(BillingExternalConsultation.attended_at)).where(
            BillingExternalConsultation.tenant_id == tenant_id,
            BillingExternalConsultation.consultorio_id == consultorio_id,
            BillingExternalConsultation.external_provider == "consultorio_movil_sync",
        )
    )


def _row_from_attended(item: AttendedConsultation) -> dict:
    return {
        "id": item.external_id,
        "fecha": item.attended_at.strftime("%d/%m/%Y %H:%M") if item.attended_at else "",
        "paciente": item.patient_name or "",
        "email": item.patient_email or "",
        "telefono": item.patient_phone or "",
        "obra social": item.insurance_name or "",
        "profesional": item.professional_name or "",
        "practica": item.practice_name or "",
        "diagnostico": item.diagnosis or "",
        "raw": item.raw_payload,
    }
