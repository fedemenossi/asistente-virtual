from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.arca_billable_item import ArcaBillableItem
from app.models.arca_invoice import ArcaInvoice
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.tenant import Tenant
from app.services.arca_service import ArcaEmissionResult, ArcaService
from app.services.billing_invoice_document_service import (
    BillingInvoiceDocumentService,
    BillingInvoiceEmailService,
)


@dataclass(frozen=True)
class BillingPreviewLine:
    consultation: BillingExternalConsultation
    item: ArcaBillableItem
    diagnosis: str
    amount: Any


class BillingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_attended_consultations(self, tenant_id: int, **filters: Any) -> list[BillingExternalConsultation]:
        stmt = select(BillingExternalConsultation).where(
            BillingExternalConsultation.tenant_id == tenant_id,
            BillingExternalConsultation.arca_invoice_id.is_(None),
        )
        dni = str(filters.get("dni") or "").strip()
        if dni:
            stmt = stmt.where(BillingExternalConsultation.patient_document.ilike(f"%{dni}%"))
        obra_social = str(filters.get("obra_social") or "").strip()
        if obra_social:
            stmt = stmt.where(BillingExternalConsultation.insurance_name.ilike(f"%{obra_social}%"))
        result = await self._session.execute(stmt.order_by(BillingExternalConsultation.attended_at.desc()))
        return list(result.scalars().all())

    async def preview_invoices(
        self,
        tenant_id: int,
        consultation_ids: list[int],
        item_id: int,
    ) -> list[BillingPreviewLine]:
        item = await self._session.scalar(
            select(ArcaBillableItem).where(
                ArcaBillableItem.id == item_id,
                ArcaBillableItem.tenant_id == tenant_id,
                ArcaBillableItem.active.is_(True),
            )
        )
        if item is None:
            return []
        result = await self._session.execute(
            select(BillingExternalConsultation).where(
                BillingExternalConsultation.tenant_id == tenant_id,
                BillingExternalConsultation.id.in_(consultation_ids),
            )
        )
        return [
            BillingPreviewLine(
                consultation=consultation,
                item=item,
                diagnosis=(consultation.diagnosis or "").strip(),
                amount=item.unit_price,
            )
            for consultation in result.scalars().all()
        ]

    async def issue_invoice(
        self,
        tenant: Tenant,
        consultation: BillingExternalConsultation,
        item: ArcaBillableItem,
    ) -> ArcaEmissionResult:
        return await ArcaService(self._session).emit_invoice_for_consultation(tenant, consultation, item)

    async def issue_batch(
        self,
        tenant: Tenant,
        pairs: list[tuple[BillingExternalConsultation, ArcaBillableItem]],
    ) -> list[ArcaEmissionResult]:
        results: list[ArcaEmissionResult] = []
        for consultation, item in pairs:
            results.append(await self.issue_invoice(tenant, consultation, item))
        return results

    async def mark_consultation_billed(
        self,
        consultation: BillingExternalConsultation,
        invoice: ArcaInvoice,
    ) -> None:
        consultation.arca_invoice_id = invoice.id
        consultation.status = "billed"

    async def send_invoice_email(self, tenant: Tenant, invoice: ArcaInvoice, to_email: str):
        document = await BillingInvoiceDocumentService(self._session).ensure_document(tenant, invoice)
        return await BillingInvoiceEmailService(self._session).send_invoice(
            tenant,
            invoice,
            to_email=to_email or document.patient_email or "",
            document=document,
        )
