from __future__ import annotations

import html
import base64
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_ba
from app.models.arca_invoice import ArcaInvoice
from app.models.arca_invoice_event import ArcaInvoiceEvent
from app.models.billing_email_log import BillingEmailLog
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.billing_setting import BillingSetting
from app.models.tenant import Tenant


class BillingInvoiceDocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class BillingInvoiceDocument:
    html: str
    pdf: bytes
    diagnosis: str
    patient_email: str | None


class BillingInvoiceDocumentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_consultation(self, invoice: ArcaInvoice) -> BillingExternalConsultation | None:
        return await self._session.scalar(
            select(BillingExternalConsultation).where(
                BillingExternalConsultation.tenant_id == invoice.tenant_id,
                BillingExternalConsultation.arca_invoice_id == invoice.id,
            )
        )

    async def build_document(
        self,
        tenant: Tenant,
        invoice: ArcaInvoice,
        *,
        consultation: BillingExternalConsultation | None = None,
        include_diagnosis: bool | None = None,
    ) -> BillingInvoiceDocument:
        if consultation is None:
            consultation = await self.get_consultation(invoice)
        if include_diagnosis is None:
            include_diagnosis = await self.include_diagnosis_in_pdf(invoice.tenant_id)
        diagnosis = extract_invoice_diagnosis(invoice, consultation) if include_diagnosis else ""
        patient_email = extract_patient_email(consultation)
        body = build_invoice_html(tenant, invoice, consultation, diagnosis)
        pdf = build_invoice_pdf(tenant, invoice, consultation, diagnosis)
        return BillingInvoiceDocument(
            html=body,
            pdf=pdf,
            diagnosis=diagnosis,
            patient_email=patient_email,
        )

    async def include_diagnosis_in_pdf(self, tenant_id: int) -> bool:
        settings = await self._session.scalar(
            select(BillingSetting).where(BillingSetting.tenant_id == tenant_id)
        )
        if settings is None:
            return True
        if hasattr(settings, "include_diagnosis_in_invoice_pdf"):
            value = getattr(settings, "include_diagnosis_in_invoice_pdf")
            if value is not None:
                return bool(value)
        return bool(getattr(settings, "diagnosis_visible_on_invoice", True))

    async def ensure_document(
        self,
        tenant: Tenant,
        invoice: ArcaInvoice,
        *,
        consultation: BillingExternalConsultation | None = None,
        force: bool = False,
    ) -> BillingInvoiceDocument:
        if consultation is None:
            consultation = await self.get_consultation(invoice)
        patient_email = extract_patient_email(consultation)
        include_diagnosis = await self.include_diagnosis_in_pdf(invoice.tenant_id)
        diagnosis = extract_invoice_diagnosis(invoice, consultation) if include_diagnosis else ""
        if (
            not force
            and invoice.document_html
            and invoice.document_pdf
        ):
            return BillingInvoiceDocument(
                html=invoice.document_html,
                pdf=bytes(invoice.document_pdf),
                diagnosis=diagnosis,
                patient_email=patient_email,
            )
        document = await self.build_document(
            tenant,
            invoice,
            consultation=consultation,
            include_diagnosis=include_diagnosis,
        )
        invoice.document_html = document.html
        invoice.document_pdf = document.pdf
        invoice.document_filename = invoice.document_filename or invoice_pdf_filename(invoice)
        invoice.document_generated_at = now_ba()
        invoice.qr_url = build_arca_qr_url(invoice)
        self._session.add(invoice)
        await self._session.flush()
        return document

    async def generate_and_store_document(
        self,
        tenant: Tenant,
        invoice: ArcaInvoice,
        *,
        user_id: int | None,
        consultation: BillingExternalConsultation | None = None,
        force: bool = False,
    ) -> BillingInvoiceDocument:
        if not invoice.cae or not invoice.cae_fch_vto:
            raise BillingInvoiceDocumentError("La factura no tiene CAE o vencimiento de CAE.")
        status = invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status)
        if status != "authorized":
            raise BillingInvoiceDocumentError("Solo se puede generar PDF de facturas autorizadas.")
        document = await self.ensure_document(
            tenant,
            invoice,
            consultation=consultation,
            force=force,
        )
        invoice.qr_url = build_arca_qr_url(invoice)
        invoice.document_filename = invoice.document_filename or invoice_pdf_filename(invoice)
        pdf_path = invoice_pdf_storage_path(invoice)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(document.pdf)
        invoice.pdf_path = str(pdf_path)
        invoice.pdf_generated_at = now_ba()
        invoice.pdf_generated_by = user_id
        self._session.add(
            ArcaInvoiceEvent(
                invoice_id=invoice.id,
                event_type="arca_invoice_pdf_regenerated" if force else "arca_invoice_pdf_generated",
                payload_json={"pdf_path": invoice.pdf_path, "qr_url": invoice.qr_url},
            )
        )
        self._session.add(invoice)
        await self._session.flush()
        return document


class BillingInvoiceEmailService:
    def __init__(self, session: AsyncSession, mailer: Any | None = None) -> None:
        self._session = session
        if mailer is None:
            from app.services.messaging_service import MessagingService

            mailer = MessagingService()
        self._mailer = mailer

    async def send_invoice(
        self,
        tenant: Tenant,
        invoice: ArcaInvoice,
        *,
        to_email: str,
        document: BillingInvoiceDocument,
    ) -> BillingEmailLog:
        recipient = (to_email or "").strip().lower()
        if not _valid_email(recipient):
            raise BillingInvoiceDocumentError("Email del paciente invalido.")
        subject = f"Factura ARCA {invoice.pto_vta}-{invoice.cbte_tipo}-{invoice.cbte_nro}"
        text_body = (
            f"Adjuntamos la factura ARCA {invoice.pto_vta}-{invoice.cbte_tipo}-{invoice.cbte_nro}.\n\n"
            f"Diagnostico: {document.diagnosis or 'No informado'}\n"
        )
        log = BillingEmailLog(
            tenant_id=tenant.id,
            invoice_id=invoice.id,
            recipient_email=recipient,
            subject=subject,
            status="pending",
        )
        self._session.add(log)
        await self._session.flush()
        try:
            self._mailer.send_email(
                recipient,
                subject,
                text_body,
                html_body=document.html,
                attachments=[
                    (
                        invoice.document_filename or invoice_pdf_filename(invoice),
                        document.pdf,
                        "application/pdf",
                    )
                ],
            )
        except Exception as exc:
            log.status = "failed"
            log.error_message = str(exc)
            raise BillingInvoiceDocumentError(str(exc)) from exc
        log.status = "sent"
        log.sent_at = now_ba()
        invoice.email_to = recipient
        invoice.email_sent_at = log.sent_at
        return log


def extract_invoice_diagnosis(
    invoice: ArcaInvoice,
    consultation: BillingExternalConsultation | None = None,
) -> str:
    if invoice.diagnosis_final_snapshot:
        return invoice.diagnosis_final_snapshot.strip()
    if invoice.diagnosis_original_snapshot:
        return invoice.diagnosis_original_snapshot.strip()
    if consultation and consultation.diagnosis:
        return consultation.diagnosis.strip()
    request = invoice.request_json or {}
    metadata = request.get("metadata") if isinstance(request, dict) else {}
    diagnosis = metadata.get("diagnosis") if isinstance(metadata, dict) else None
    if diagnosis:
        return str(diagnosis).strip()
    detail = _invoice_detail(invoice)
    diagnosis = detail.get("Diagnostico") or detail.get("diagnostico")
    return str(diagnosis or "").strip()


def extract_patient_email(consultation: BillingExternalConsultation | None) -> str | None:
    if consultation is None:
        return None
    if consultation.patient_email and _valid_email(consultation.patient_email):
        return consultation.patient_email.strip().lower()
    payload = consultation.raw_payload_json or {}
    for key in ("email", "patient_email", "paciente_email", "mail"):
        value = _find_key(payload, key)
        if value and _valid_email(str(value)):
            return str(value).strip().lower()
    return None


def build_invoice_html(
    tenant: Tenant,
    invoice: ArcaInvoice,
    consultation: BillingExternalConsultation | None,
    diagnosis: str,
) -> str:
    detail = _invoice_detail(invoice)
    metadata = invoice.request_json.get("metadata", {}) if isinstance(invoice.request_json, dict) else {}
    description = (
        detail.get("Descripcion")
        or detail.get("descripcion")
        or metadata.get("description")
        or metadata.get("descripcion")
        or "Consulta medica"
    )
    patient_name = consultation.patient_name if consultation else "-"
    patient_document = consultation.patient_document if consultation else invoice.doc_nro
    fiscal = tenant.arca_settings or {}
    qr_url = invoice.qr_url or build_arca_qr_url(invoice)
    copy_label = "ORIGINAL"
    environment = str(invoice.environment or "").lower()
    title = f"Factura ARCA {invoice.pto_vta}-{invoice.cbte_tipo}-{invoice.cbte_nro}"
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; color: #0f172a; margin: 32px; }}
    .copy {{ text-align: center; font-size: 20px; font-weight: 700; letter-spacing: 0.08em; }}
    .watermark {{ color: #b91c1c; font-weight: 700; text-align: center; margin: 8px 0; }}
    .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #0f172a; padding-bottom: 16px; }}
    .box {{ border: 1px solid #cbd5e1; padding: 14px; margin-top: 18px; }}
    .label {{ color: #64748b; font-size: 12px; text-transform: uppercase; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 18px; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 10px; text-align: left; }}
    th {{ background: #f8fafc; }}
    .diagnosis {{ white-space: pre-wrap; font-weight: 700; }}
  </style>
</head>
<body>
  <div class="copy">{copy_label}</div>
  {'<div class="watermark">HOMOLOGACION - SIN VALIDEZ FISCAL</div>' if environment == 'homo' else ''}
  <div class="header">
    <div>
      <p class="label">Emisor</p>
      <h1>{html.escape(str(fiscal.get("fiscal_name") or tenant.nombre))}</h1>
      <p>CUIT {html.escape(invoice.represented_cuit)}</p>
      <p>{html.escape(str(fiscal.get("fiscal_address") or ""))}</p>
    </div>
    <div>
      <p class="label">Comprobante</p>
      <h2>{html.escape(title)}</h2>
      <p>Fecha: {invoice.cbte_fch or "-"}</p>
      <p>CAE: {html.escape(invoice.cae or "-")}</p>
      <p>Vencimiento CAE: {invoice.cae_fch_vto or "-"}</p>
    </div>
  </div>
  <div class="box">
    <p class="label">Paciente</p>
    <p>{html.escape(str(patient_name or "-"))}</p>
    <p>Documento: {html.escape(str(patient_document or "-"))}</p>
  </div>
  <table>
    <thead><tr><th>Descripcion</th><th>Diagnostico</th><th>Importe</th></tr></thead>
    <tbody>
      <tr>
        <td>{html.escape(str(description))}</td>
        <td class="diagnosis">{html.escape(diagnosis or "No informado")}</td>
        <td>{html.escape(_money(invoice.imp_total))} {html.escape(invoice.mon_id)}</td>
      </tr>
    </tbody>
  </table>
  <div class="box">
    <p class="label">Diagnostico informado en factura</p>
    <p class="diagnosis">{html.escape(diagnosis or "No informado")}</p>
  </div>
  <div class="box">
    <p class="label">QR ARCA</p>
    <p style="word-break: break-all;">{html.escape(qr_url)}</p>
  </div>
</body>
</html>"""


def build_invoice_pdf(
    tenant: Tenant,
    invoice: ArcaInvoice,
    consultation: BillingExternalConsultation | None,
    diagnosis: str,
) -> bytes:
    fiscal = tenant.arca_settings or {}
    patient = consultation.patient_name if consultation else "-"
    lines = [
        f"Factura ARCA {invoice.pto_vta}-{invoice.cbte_tipo}-{invoice.cbte_nro}",
        "ORIGINAL",
        "HOMOLOGACION - SIN VALIDEZ FISCAL" if str(invoice.environment or "").lower() == "homo" else "",
        f"Emisor: {fiscal.get('fiscal_name') or tenant.nombre}",
        f"CUIT: {invoice.represented_cuit}",
        f"Paciente: {patient or '-'}",
        f"Documento: {(consultation.patient_document if consultation else invoice.doc_nro) or '-'}",
        f"Importe: {_money(invoice.imp_total)} {invoice.mon_id}",
        f"CAE: {invoice.cae or '-'}",
        f"Vencimiento CAE: {invoice.cae_fch_vto or '-'}",
        f"QR ARCA: {invoice.qr_url or build_arca_qr_url(invoice)}",
        "Diagnostico:",
        diagnosis or "No informado",
    ]
    return _simple_pdf(lines)


def invoice_pdf_filename(invoice: ArcaInvoice) -> str:
    cbte_nro = invoice.cbte_nro or invoice.id
    return f"factura-arca-{invoice.pto_vta}-{invoice.cbte_tipo}-{cbte_nro}.pdf"


def invoice_pdf_storage_path(invoice: ArcaInvoice) -> Path:
    return Path.cwd() / "storage" / "invoices" / str(invoice.tenant_id) / str(invoice.id) / invoice_pdf_filename(invoice)


def build_arca_qr_url(invoice: ArcaInvoice) -> str:
    doc_digits = "".join(ch for ch in str(invoice.doc_nro or "") if ch.isdigit())
    cae_digits = "".join(ch for ch in str(invoice.cae or "") if ch.isdigit())
    payload = {
        "ver": 1,
        "fecha": invoice.cbte_fch.isoformat() if invoice.cbte_fch else "",
        "cuit": int(invoice.represented_cuit or 0),
        "ptoVta": int(invoice.pto_vta or 0),
        "tipoCmp": int(invoice.cbte_tipo or 0),
        "nroCmp": int(invoice.cbte_nro or 0),
        "importe": float(invoice.imp_total or 0),
        "moneda": invoice.mon_id or "PES",
        "ctz": float(invoice.mon_cotiz or 1),
        "tipoDocRec": int(invoice.doc_tipo or 99),
        "nroDocRec": int(doc_digits or 0),
        "tipoCodAut": "E",
        "codAut": int(cae_digits or 0),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return f"https://www.afip.gob.ar/fe/qr/?p={encoded}"


def _invoice_detail(invoice: ArcaInvoice) -> dict[str, Any]:
    request = invoice.request_json or {}
    if not isinstance(request, dict):
        return {}
    det = request.get("FeDetReq") or request.get("feDetReq") or {}
    if isinstance(det, dict):
        det = det.get("FECAEDetRequest") or det.get("feCaeDetRequest") or det
    if isinstance(det, list):
        det = det[0] if det else {}
    return det if isinstance(det, dict) else {}


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if str(current_key).lower() == key:
                return current_value
            found = _find_key(current_value, key)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, key)
            if found:
                return found
    return None


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def _money(value: Any) -> str:
    amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    return f"{amount:.2f}"


def _simple_pdf(lines: list[str]) -> bytes:
    stream = BytesIO()
    offsets: list[int] = []

    def write(data: str) -> None:
        stream.write(data.encode("latin-1", errors="replace"))

    write("%PDF-1.4\n")
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    content = ["BT", "/F1 12 Tf", "50 790 Td"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            content.append("0 -18 Td")
        content.append(f"({escaped}) Tj")
    content.append("ET")
    content_stream = "\n".join(content)
    objects.append(
        f"5 0 obj << /Length {len(content_stream.encode('latin-1', errors='replace'))} >> stream\n"
        f"{content_stream}\nendstream endobj\n"
    )
    for obj in objects:
        offsets.append(stream.tell())
        write(obj)
    xref_pos = stream.tell()
    write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n")
    for offset in offsets:
        write(f"{offset:010d} 00000 n \n")
    write(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    )
    return stream.getvalue()
