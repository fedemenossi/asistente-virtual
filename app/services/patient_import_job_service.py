from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_ba
from app.services.patient_sync_service import PatientSyncRow, PatientSyncService

logger = logging.getLogger(__name__)


@dataclass
class PatientImportJob:
    id: str
    tenant_id: int
    filename: str
    total_rows: int
    status: str = "queued"
    processed: int = 0
    created: int = 0
    updated: int = 0
    existing: int = 0
    missing_document: int = 0
    errors: int = 0
    error_message: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: now_ba().replace(tzinfo=None))

    @property
    def percent(self) -> int:
        if self.total_rows <= 0:
            return 100 if self.status in {"completed", "failed"} else 0
        return min(100, int((self.processed / self.total_rows) * 100))

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "filename": self.filename,
            "status": self.status,
            "total_rows": self.total_rows,
            "processed": self.processed,
            "percent": self.percent,
            "created": self.created,
            "updated": self.updated,
            "existing": self.existing,
            "missing_document": self.missing_document,
            "errors": self.errors,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat(sep=" ") if self.started_at else "",
            "finished_at": self.finished_at.isoformat(sep=" ") if self.finished_at else "",
            "created_at": self.created_at.isoformat(sep=" "),
        }


_jobs: dict[str, PatientImportJob] = {}
_tenant_latest: dict[int, str] = {}


def start_patient_csv_import_job(tenant_id: int, rows: list[PatientSyncRow], filename: str) -> PatientImportJob:
    job = PatientImportJob(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        filename=filename,
        total_rows=len(rows),
    )
    _jobs[job.id] = job
    _tenant_latest[tenant_id] = job.id
    asyncio.create_task(_run_patient_csv_import_job(job.id, rows))
    return job


def get_patient_import_job(job_id: str, tenant_id: int) -> PatientImportJob | None:
    job = _jobs.get(job_id)
    if job is None or job.tenant_id != tenant_id:
        return None
    return job


def get_latest_patient_import_job(tenant_id: int) -> PatientImportJob | None:
    job_id = _tenant_latest.get(tenant_id)
    return get_patient_import_job(job_id, tenant_id) if job_id else None


async def _run_patient_csv_import_job(job_id: str, rows: list[PatientSyncRow]) -> None:
    job = _jobs[job_id]
    job.status = "running"
    job.started_at = now_ba().replace(tzinfo=None)

    def progress(
        processed: int,
        created: int,
        updated: int,
        existing: int,
        missing_document: int,
        errors: int,
    ) -> None:
        job.processed = processed
        job.created = created
        job.updated = updated
        job.existing = existing
        job.missing_document = missing_document
        job.errors = errors

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await PatientSyncService(session).sync_rows(
                    job.tenant_id,
                    rows,
                    sync_source="csv",
                    log_source=job.filename,
                    update_existing=True,
                    progress_callback=progress,
                )
                job.processed = result.total_rows
                job.created = result.created
                job.updated = result.updated
                job.existing = result.existing
                job.missing_document = result.missing_document
                job.errors = result.errors
        job.status = "completed" if job.errors == 0 else "completed_with_errors"
    except Exception as exc:
        logger.exception("patient_csv_import_job_failed", extra={"job_id": job.id, "tenant_id": job.tenant_id})
        job.status = "failed"
        job.error_message = str(exc)
    finally:
        job.finished_at = now_ba().replace(tzinfo=None)
