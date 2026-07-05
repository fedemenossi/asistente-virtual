from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_ba
from app.models.arca_billable_item import ArcaBillableItem
from app.models.arca_invoice import ArcaInvoice
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.tenant import Tenant
from app.services.arca_service import ArcaEmissionError, ArcaService
from app.services.billing_service import BillingService

logger = logging.getLogger(__name__)


@dataclass
class BillingEmissionJob:
    id: str
    tenant_id: int
    total: int
    status: str = "queued"
    processed: int = 0
    success: int = 0
    failed: int = 0
    emailed: int = 0
    error_message: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: now_ba().replace(tzinfo=None))

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 100 if self.status in {"completed", "failed"} else 0
        return min(100, int((self.processed / self.total) * 100))

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "percent": self.percent,
            "success": self.success,
            "failed": self.failed,
            "emailed": self.emailed,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat(sep=" ") if self.started_at else "",
            "finished_at": self.finished_at.isoformat(sep=" ") if self.finished_at else "",
        }


_jobs: dict[str, BillingEmissionJob] = {}
_tenant_latest: dict[int, str] = {}


def start_billing_emission_job(tenant_id: int, consultation_ids: list[int]) -> BillingEmissionJob:
    job = BillingEmissionJob(id=uuid.uuid4().hex, tenant_id=tenant_id, total=len(consultation_ids))
    _jobs[job.id] = job
    _tenant_latest[tenant_id] = job.id
    asyncio.create_task(_run_billing_emission_job(job.id, consultation_ids))
    return job


def get_billing_emission_job(job_id: str, tenant_id: int) -> BillingEmissionJob | None:
    job = _jobs.get(job_id)
    if job is None or job.tenant_id != tenant_id:
        return None
    return job


def get_latest_billing_emission_job(tenant_id: int) -> BillingEmissionJob | None:
    job_id = _tenant_latest.get(tenant_id)
    return get_billing_emission_job(job_id, tenant_id) if job_id else None


async def _run_billing_emission_job(job_id: str, consultation_ids: list[int]) -> None:
    job = _jobs[job_id]
    job.status = "running"
    job.started_at = now_ba().replace(tzinfo=None)
    try:
        async with AsyncSessionLocal() as session:
            tenant = await session.get(Tenant, job.tenant_id)
            if tenant is None:
                raise RuntimeError("Tenant no encontrado")
            await session.commit()
            for consultation_id in consultation_ids:
                try:
                    invoice_id: int | None = None
                    should_send_email = False
                    email_to = ""
                    async with session.begin():
                        consultation = await session.scalar(
                            select(BillingExternalConsultation).where(
                                BillingExternalConsultation.id == consultation_id,
                                BillingExternalConsultation.tenant_id == job.tenant_id,
                            )
                        )
                        if consultation is None:
                            raise ArcaEmissionError("Consulta no encontrada")
                        item = await session.scalar(
                            select(ArcaBillableItem).where(
                                ArcaBillableItem.id == consultation.billing_item_id,
                                ArcaBillableItem.tenant_id == job.tenant_id,
                                ArcaBillableItem.active.is_(True),
                            )
                        )
                        if item is None:
                            raise ArcaEmissionError("Item facturable invalido")
                        result = await ArcaService(session).emit_invoice_for_consultation(
                            tenant,
                            consultation,
                            item,
                            amount_override=consultation.amount,
                            send_email=consultation.send_email,
                        )
                        invoice_id = result.invoice.id
                        should_send_email = bool(consultation.send_email)
                        email_to = consultation.patient_email or result.invoice.email_to or ""
                    if should_send_email and not email_to and invoice_id is not None:
                        job.error_message = f"Email factura #{invoice_id}: paciente sin email destino."
                        logger.warning(
                            "billing_emission_job_email_skipped",
                            extra={
                                "job_id": job.id,
                                "tenant_id": job.tenant_id,
                                "invoice_id": invoice_id,
                                "reason": "missing_recipient",
                            },
                        )
                    if should_send_email and email_to and invoice_id is not None:
                        try:
                            async with session.begin():
                                invoice = await session.get(ArcaInvoice, invoice_id)
                                if invoice is not None:
                                    await BillingService(session).send_invoice_email(tenant, invoice, email_to)
                                    job.emailed += 1
                        except Exception as exc:
                            error_message = str(exc) or exc.__class__.__name__
                            logger.warning(
                                "billing_emission_job_email_failed",
                                extra={
                                    "job_id": job.id,
                                    "tenant_id": job.tenant_id,
                                    "invoice_id": invoice_id,
                                    "billing_error_type": exc.__class__.__name__,
                                    "billing_error_message": error_message,
                                },
                                exc_info=True,
                            )
                            job.error_message = f"Email factura #{invoice_id}: {error_message}"
                    job.success += 1
                except Exception as exc:
                    error_message = str(exc) or exc.__class__.__name__
                    logger.warning(
                        "billing_emission_job_row_failed",
                        extra={
                            "job_id": job.id,
                            "tenant_id": job.tenant_id,
                            "consultation_id": consultation_id,
                            "billing_error_type": exc.__class__.__name__,
                            "billing_error_message": error_message,
                        },
                        exc_info=True,
                    )
                    job.failed += 1
                    job.error_message = f"Consulta #{consultation_id}: {error_message}"
                finally:
                    job.processed += 1
        job.status = "completed" if job.failed == 0 else "completed_with_errors"
    except Exception as exc:
        error_message = str(exc) or exc.__class__.__name__
        logger.exception(
            "billing_emission_job_failed",
            extra={
                "job_id": job.id,
                "tenant_id": job.tenant_id,
                "billing_error_type": exc.__class__.__name__,
                "billing_error_message": error_message,
            },
        )
        job.status = "failed"
        job.error_message = error_message
    finally:
        job.finished_at = now_ba().replace(tzinfo=None)
