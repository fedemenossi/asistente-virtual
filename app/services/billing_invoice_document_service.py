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
from urllib.parse import quote

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
        subject = build_invoice_email_subject(tenant, invoice)
        text_body = build_invoice_email_text(tenant, invoice, document)
        html_body = build_invoice_email_html(tenant, invoice, document)
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
                html_body=html_body,
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


def build_invoice_email_subject(tenant: Tenant, invoice: ArcaInvoice) -> str:
    template = str((tenant.arca_settings or {}).get("email_subject_template") or "").strip()
    if not template:
        template = "Factura electronica {numero}"
    return _render_template(template, _email_context(tenant, invoice))


def build_invoice_email_text(tenant: Tenant, invoice: ArcaInvoice, document: BillingInvoiceDocument) -> str:
    template = str((tenant.arca_settings or {}).get("email_body_template") or "").strip()
    context = _email_context(tenant, invoice, diagnosis=document.diagnosis)
    if template:
        return _render_template(template, context)
    diagnosis_line = f"Diagnostico: {document.diagnosis}\n" if document.diagnosis else ""
    return (
        f"Hola,\n\n"
        f"Adjuntamos la factura electronica {context['numero']} por {context['importe']} {context['moneda']}.\n\n"
        f"CAE: {context['cae']}\n"
        f"Vencimiento CAE: {context['cae_vto']}\n\n"
        f"{diagnosis_line}"
        f"Saludos,\n{context['emisor']}\n"
    )


def build_invoice_email_html(tenant: Tenant, invoice: ArcaInvoice, document: BillingInvoiceDocument) -> str:
    context = _email_context(tenant, invoice, diagnosis=document.diagnosis)
    diagnosis_block = ""
    if document.diagnosis:
        diagnosis_block = f"""
          <tr>
            <td style="padding:10px 0;color:#64748b;">Diagnostico</td>
            <td style="padding:10px 0;text-align:right;font-weight:600;color:#0f172a;">{html.escape(document.diagnosis)}</td>
          </tr>
        """
    body_text = str((tenant.arca_settings or {}).get("email_body_template") or "").strip()
    intro = _render_template(body_text, context) if body_text else "Adjuntamos la factura electronica correspondiente."
    return f"""<!doctype html>
<html lang="es">
<body style="margin:0;background:#f8fafc;font-family:Arial,sans-serif;color:#0f172a;">
  <div style="max-width:680px;margin:0 auto;padding:28px;">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
      <div style="padding:22px 24px;border-bottom:1px solid #e5e7eb;background:#f9fafb;">
        <p style="margin:0 0 6px;color:#64748b;font-size:12px;text-transform:uppercase;letter-spacing:.08em;">Factura electronica</p>
        <h1 style="margin:0;font-size:22px;color:#111827;">{html.escape(context['numero'])}</h1>
      </div>
      <div style="padding:24px;">
        <p style="margin:0 0 18px;line-height:1.5;color:#334155;">{html.escape(intro).replace(chr(10), '<br>')}</p>
        <table style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">
          <tr>
            <td style="padding:10px 0;color:#64748b;">Emisor</td>
            <td style="padding:10px 0;text-align:right;font-weight:600;color:#0f172a;">{html.escape(context['emisor'])}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#64748b;">Comprobante</td>
            <td style="padding:10px 0;text-align:right;font-weight:600;color:#0f172a;">{html.escape(context['numero'])}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#64748b;">Importe</td>
            <td style="padding:10px 0;text-align:right;font-weight:700;color:#0f172a;">{html.escape(context['importe'])} {html.escape(context['moneda'])}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#64748b;">CAE</td>
            <td style="padding:10px 0;text-align:right;font-weight:600;color:#0f172a;">{html.escape(context['cae'])}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#64748b;">Vencimiento CAE</td>
            <td style="padding:10px 0;text-align:right;font-weight:600;color:#0f172a;">{html.escape(context['cae_vto'])}</td>
          </tr>
          {diagnosis_block}
        </table>
        <p style="margin:18px 0 0;color:#64748b;font-size:13px;">El comprobante fiscal se encuentra adjunto en formato PDF.</p>
      </div>
    </div>
  </div>
</body>
</html>"""


def build_invoice_pdf(
    tenant: Tenant,
    invoice: ArcaInvoice,
    consultation: BillingExternalConsultation | None,
    diagnosis: str,
) -> bytes:
    try:
        return _fiscal_pdf(tenant, invoice, consultation, diagnosis)
    except ImportError as exc:
        raise BillingInvoiceDocumentError("Faltan dependencias para generar PDF fiscal: reportlab y qrcode.") from exc


def _fiscal_pdf(
    tenant: Tenant,
    invoice: ArcaInvoice,
    consultation: BillingExternalConsultation | None,
    diagnosis: str,
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import qrcode

    fiscal = tenant.arca_settings or {}
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=0)
    width, height = A4
    qr_url = invoice.qr_url or build_arca_qr_url(invoice)
    qr_image = qrcode.make(qr_url).get_image().convert("RGB")
    qr_reader = ImageReader(qr_image)
    copies = ("ORIGINAL", "DUPLICADO", "TRIPLICADO")

    for copy_label in copies:
        _draw_invoice_page(
            pdf,
            width,
            height,
            tenant,
            invoice,
            consultation,
            diagnosis,
            fiscal,
            qr_reader,
            qr_url,
            copy_label,
        )
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _draw_invoice_page(
    pdf: Any,
    width: float,
    height: float,
    tenant: Tenant,
    invoice: ArcaInvoice,
    consultation: BillingExternalConsultation | None,
    diagnosis: str,
    fiscal: dict[str, Any],
    qr_reader: Any,
    qr_url: str,
    copy_label: str,
) -> None:
    from reportlab.lib import colors

    margin = 32
    top = height - margin
    left = margin
    right = width - margin
    center = width / 2
    letter = _invoice_letter(invoice.cbte_tipo)
    title = f"FACTURA {letter}"
    cbte_nro = int(invoice.cbte_nro or 0)
    pto_vta = int(invoice.pto_vta or 0)
    patient_name = consultation.patient_name if consultation else "-"
    patient_document = consultation.patient_document if consultation else invoice.doc_nro
    description = _invoice_description(invoice)
    emisor_name = str(fiscal.get("fiscal_name") or tenant.nombre or "-")
    emisor_address = str(fiscal.get("fiscal_address") or fiscal.get("address") or "")
    emisor_iva = str(fiscal.get("fiscal_iva_condition") or fiscal.get("iva_condition") or "Responsable Monotributo")
    ingresos_brutos = str(fiscal.get("gross_income") or fiscal.get("ingresos_brutos") or "EXENTO")
    inicio_actividades = str(fiscal.get("activity_start_date") or fiscal.get("inicio_actividades") or "")
    receptor_iva = _receiver_tax_condition_label(invoice)

    pdf.setTitle(invoice_pdf_filename(invoice))
    pdf.setStrokeColor(colors.HexColor("#111827"))
    pdf.setLineWidth(0.8)

    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawCentredString(center, top, copy_label)
    if str(invoice.environment or "").lower() == "homo":
        pdf.setFillColor(colors.HexColor("#b91c1c"))
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawCentredString(center, top - 16, "HOMOLOGACION - SIN VALIDEZ FISCAL")
        pdf.setFillColor(colors.black)

    y = top - 34
    pdf.rect(left, y - 94, right - left, 94, stroke=1, fill=0)
    pdf.line(center, y, center, y - 94)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(left + 12, y - 22, _clip(emisor_name, 32))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(left + 12, y - 40, _clip(emisor_address, 58))
    pdf.drawString(left + 12, y - 56, f"Condicion frente al IVA: {_clip(emisor_iva, 36)}")
    pdf.drawString(left + 12, y - 72, f"CUIT: {invoice.represented_cuit}")
    pdf.drawString(left + 12, y - 88, f"Ingresos Brutos: {_clip(ingresos_brutos, 24)}")
    if inicio_actividades:
        pdf.drawString(left + 200, y - 88, f"Inicio actividades: {_format_date_display(inicio_actividades)}")

    box_size = 34
    pdf.setFillColor(colors.white)
    pdf.rect(center - box_size / 2, y - 44, box_size, box_size, stroke=1, fill=1)
    pdf.setFillColor(colors.black)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawCentredString(center, y - 35, letter)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(center + 24, y - 22, title)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(center + 24, y - 40, f"COD. {int(invoice.cbte_tipo or 0):03d}")
    pdf.drawString(center + 24, y - 56, f"Punto de Venta: {pto_vta:05d}")
    pdf.drawString(center + 170, y - 56, f"Comp. Nro: {cbte_nro:08d}")
    pdf.drawString(center + 24, y - 72, f"Fecha de Emision: {_format_date_display(invoice.cbte_fch)}")

    y -= 112
    pdf.rect(left, y - 82, right - left, 82, stroke=1, fill=0)
    pdf.setFont("Helvetica", 8)
    pdf.drawString(left + 10, y - 16, f"Periodo Facturado Desde: {_format_date_display(invoice.cbte_fch)}")
    pdf.drawString(left + 190, y - 16, f"Hasta: {_format_date_display(invoice.cbte_fch)}")
    pdf.drawString(left + 310, y - 16, f"Fecha de Vto. para el pago: {_format_date_display(invoice.cbte_fch)}")
    pdf.drawString(left + 10, y - 34, "Condicion de venta: Contado")
    pdf.drawString(left + 10, y - 52, f"Apellido y Nombre / Razon Social: {_clip(patient_name or '-', 60)}")
    pdf.drawString(left + 10, y - 70, f"{_doc_label(invoice.doc_tipo)}: {patient_document or '-'}")
    pdf.drawString(left + 310, y - 70, f"Condicion frente al IVA: {_clip(receptor_iva, 30)}")

    y -= 104
    diagnosis_text = diagnosis or "No informado"
    table_h = 128
    pdf.rect(left, y - table_h, right - left, table_h, stroke=1, fill=0)
    pdf.setFillColor(colors.HexColor("#f3f4f6"))
    pdf.rect(left, y - 22, right - left, 22, stroke=0, fill=1)
    pdf.setFillColor(colors.black)
    pdf.setFont("Helvetica-Bold", 8)
    columns = [
        (left, left + 244, "Codigo Producto / Servicio", "left"),
        (left + 244, left + 302, "Cantidad", "right"),
        (left + 302, left + 372, "U. Medida", "left"),
        (left + 372, left + 452, "Precio Unit.", "right"),
        (left + 452, left + 506, "% Bonif", "right"),
        (left + 506, right, "Subtotal", "right"),
    ]
    for x0, _, _, _ in columns[1:]:
        pdf.setStrokeColor(colors.HexColor("#d1d5db"))
        pdf.line(x0, y, x0, y - table_h)
    pdf.setStrokeColor(colors.HexColor("#111827"))
    for x0, x1, text, align in columns:
        if align == "right":
            pdf.drawRightString(x1 - 8, y - 15, text)
        else:
            pdf.drawString(x0 + 8, y - 15, text)
    pdf.setFont("Helvetica", 9)
    row_y = y - 42
    pdf.drawString(left + 8, row_y, _clip(description, 40))
    pdf.drawRightString(left + 294, row_y, "1,00")
    pdf.drawString(left + 310, row_y, "unidades")
    pdf.drawRightString(left + 444, row_y, _money_ar(invoice.imp_total))
    pdf.drawRightString(left + 498, row_y, "0,00")
    pdf.drawRightString(right - 8, row_y, _money_ar(invoice.imp_total))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(left + 8, row_y - 28, "Diagnostico:")
    pdf.setFont("Helvetica", 8)
    _draw_wrapped(pdf, diagnosis_text, left + 70, row_y - 28, 360, 10, max_lines=3)

    y -= table_h + 20
    pdf.rect(center + 40, y - 78, right - center - 40, 78, stroke=1, fill=0)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(center + 52, y - 18, "Subtotal: $")
    pdf.drawRightString(right - 12, y - 18, _money_ar(invoice.imp_total))
    pdf.drawString(center + 52, y - 38, "Importe Otros Tributos: $")
    pdf.drawRightString(right - 12, y - 38, _money_ar(invoice.imp_trib))
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(center + 52, y - 62, "Importe Total: $")
    pdf.drawRightString(right - 12, y - 62, _money_ar(invoice.imp_total))

    y -= 110
    qr_size = 92
    pdf.drawImage(qr_reader, left, y - qr_size + 12, width=qr_size, height=qr_size, mask="auto")
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left + 112, y - 8, "CAE Nro:")
    pdf.drawString(left + 112, y - 28, "Fecha de Vto. de CAE:")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(left + 250, y - 8, str(invoice.cae or "-"))
    pdf.drawString(left + 250, y - 28, _format_date_display(invoice.cae_fch_vto))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left + 112, y - 54, "Comprobante Autorizado")
    pdf.setFont("Helvetica", 8)
    pdf.drawString(left + 112, y - 72, "Esta Agencia no se responsabiliza por los datos ingresados en el detalle de la operacion")
    pdf.setFont("Helvetica", 8)
    footer = str(fiscal.get("invoice_footer") or fiscal.get("footer") or '"Medica especialista en Ginecologia y Obstetricia  M.N. 122.674"')
    pdf.drawCentredString(center, 36, _clip(footer, 90))
    pdf.drawRightString(right, 22, "Pag. 1/1")


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
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return f"https://www.afip.gob.ar/fe/qr/?p={quote(encoded, safe='')}"


def _invoice_letter(cbte_tipo: Any) -> str:
    mapping = {
        1: "A",
        2: "A",
        3: "A",
        6: "B",
        7: "B",
        8: "B",
        11: "C",
        12: "C",
        13: "C",
    }
    try:
        return mapping.get(int(cbte_tipo or 0), "")
    except (TypeError, ValueError):
        return ""


def _doc_label(doc_tipo: Any) -> str:
    try:
        value = int(doc_tipo or 0)
    except (TypeError, ValueError):
        value = 0
    return {
        80: "CUIT",
        86: "CUIL",
        96: "DNI",
        99: "Documento",
    }.get(value, "Documento")


def _receiver_tax_condition_label(invoice: ArcaInvoice) -> str:
    request = invoice.request_json or {}
    value = None
    if isinstance(request, dict):
        metadata = request.get("metadata") or {}
        if isinstance(metadata, dict):
            value = metadata.get("receiver_tax_condition_id")
        detail = _invoice_detail(invoice)
        value = value or detail.get("CondicionIVAReceptorId")
    try:
        value_int = int(value or 5)
    except (TypeError, ValueError):
        value_int = 5
    return {
        1: "IVA Responsable Inscripto",
        4: "IVA Sujeto Exento",
        5: "Consumidor Final",
        6: "Responsable Monotributo",
        7: "Sujeto No Categorizado",
        15: "IVA No Alcanzado",
    }.get(value_int, "Consumidor Final")


def _invoice_description(invoice: ArcaInvoice) -> str:
    detail = _invoice_detail(invoice)
    metadata = invoice.request_json.get("metadata", {}) if isinstance(invoice.request_json, dict) else {}
    return str(
        detail.get("Descripcion")
        or detail.get("descripcion")
        or metadata.get("description")
        or metadata.get("descripcion")
        or "Consulta"
    )


def _money_ar(value: Any) -> str:
    amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    return f"{amount:.2f}".replace(".", ",")


def _format_date_display(value: Any) -> str:
    if not value:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[6:8]}/{text[4:6]}/{text[0:4]}"
    return text


def _clip(value: Any, max_len: int) -> str:
    text = str(value or "")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _draw_wrapped(pdf: Any, text: str, x: float, y: float, max_width: float, line_height: float, *, max_lines: int) -> None:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if pdf.stringWidth(candidate, pdf._fontname, pdf._fontsize) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    for index, line in enumerate(lines[:max_lines]):
        pdf.drawString(x, y - index * line_height, line)


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


def _email_context(
    tenant: Tenant,
    invoice: ArcaInvoice,
    *,
    diagnosis: str = "",
) -> dict[str, str]:
    numero = f"{int(invoice.pto_vta or 0):05d}-{int(invoice.cbte_nro or 0):08d}"
    fiscal = tenant.arca_settings or {}
    return {
        "numero": numero,
        "pto_vta": str(invoice.pto_vta or ""),
        "cbte_tipo": str(invoice.cbte_tipo or ""),
        "cbte_nro": str(invoice.cbte_nro or ""),
        "importe": _money(invoice.imp_total),
        "moneda": invoice.mon_id or "PES",
        "cae": invoice.cae or "-",
        "cae_vto": _format_date_display(invoice.cae_fch_vto),
        "emisor": str(fiscal.get("fiscal_name") or tenant.nombre or ""),
        "diagnostico": diagnosis or "",
        "diagnosis": diagnosis or "",
    }


def _render_template(template: str, context: dict[str, str]) -> str:
    class SafeDict(dict):
        def __missing__(self, key):
            return "{" + str(key) + "}"

    try:
        return template.format_map(SafeDict(context))
    except Exception:
        return template


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
