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
    from reportlab.lib.units import mm

    margin_x = 12 * mm
    margin_y = 10 * mm
    margin = margin_x
    top = height - margin_y
    left = margin_x
    right = width - margin_x
    center = width / 2
    letter = _invoice_letter(invoice.cbte_tipo)
    title = f"FACTURA {letter}".strip()
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
    professional_legend = str(fiscal.get("professional_legend") or fiscal.get("invoice_footer") or fiscal.get("footer") or "")
    receptor_iva = _receiver_tax_condition_label(invoice)
    amount = Decimal(str(invoice.imp_total or "0")).quantize(Decimal("0.01"))

    pdf.setTitle(invoice_pdf_filename(invoice))
    pdf.setStrokeColor(colors.HexColor("#111111"))
    pdf.setLineWidth(0.6)

    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawCentredString(center, top, copy_label)
    if str(invoice.environment or "").lower() == "homo":
        pdf.setFillColor(colors.HexColor("#b91c1c"))
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawCentredString(center, top - 16, "HOMOLOGACION - SIN VALIDEZ FISCAL")
        pdf.setFillColor(colors.black)

    header_top = top - 18
    header_h = 43 * mm
    side_w = (right - left) / 2
    divider_x = center
    header_bottom = header_top - header_h

    pdf.setLineWidth(0.6)
    pdf.rect(left, header_bottom, right - left, header_h, stroke=1, fill=0)
    pdf.line(divider_x, header_top, divider_x, header_bottom)

    voucher_w = 20 * mm
    voucher_h = 21 * mm
    pdf.setFillColor(colors.white)
    pdf.rect(center - voucher_w / 2, header_top - voucher_h, voucher_w, voucher_h, stroke=1, fill=1)
    pdf.setFillColor(colors.black)
    pdf.setFont("Helvetica-Bold", 26)
    pdf.drawCentredString(center, header_top - 30, letter)
    pdf.setFont("Helvetica-Bold", 7.5)
    pdf.drawCentredString(center, header_top - 47, f"COD. {int(invoice.cbte_tipo or 0):03d}")

    left_pad = 4 * mm
    right_pad = 4 * mm
    left_content_x = left + left_pad
    right_content_x = divider_x + (13 * mm)
    header_text_top = header_top - 7 * mm

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left_content_x, header_text_top, _clip(emisor_name, 36))
    _draw_field(pdf, left_content_x, header_text_top - 21, "Razon Social:", emisor_name, label_w=26 * mm, value_max=120)
    _draw_field(pdf, left_content_x, header_text_top - 39, "Domicilio Comercial:", emisor_address, label_w=37 * mm, value_max=96)
    _draw_field(pdf, left_content_x, header_text_top - 57, "Condicion frente al IVA:", emisor_iva, label_w=43 * mm, value_max=82)

    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(right_content_x, header_text_top, title)
    row_y = header_text_top - 23
    _draw_field(pdf, right_content_x, row_y, "Punto de Venta:", f"{pto_vta:05d}", label_w=27 * mm, value_max=38)
    _draw_field(pdf, right_content_x + 45 * mm, row_y, "Comp. Nro:", f"{cbte_nro:08d}", label_w=20 * mm, value_max=48)
    _draw_field(pdf, right_content_x, row_y - 18, "Fecha de Emision:", _format_date_display(invoice.cbte_fch), label_w=34 * mm, value_max=66)
    _draw_field(pdf, right_content_x, row_y - 36, "CUIT:", str(invoice.represented_cuit or "-"), label_w=12 * mm, value_max=76)
    _draw_field(pdf, right_content_x, row_y - 54, "Ingresos Brutos:", ingresos_brutos, label_w=30 * mm, value_max=72)
    _draw_field(pdf, right_content_x, row_y - 72, "Fecha de Inicio de Actividades:", _format_date_display(inicio_actividades) if inicio_actividades else "-", label_w=56 * mm, value_max=52)

    period_h = 8 * mm
    period_top = header_bottom
    period_bottom = period_top - period_h
    pdf.rect(left, period_bottom, right - left, period_h, stroke=1, fill=0)
    period_cols = [left, left + (right - left) / 3, left + 2 * (right - left) / 3, right]
    pdf.line(period_cols[1], period_top, period_cols[1], period_bottom)
    pdf.line(period_cols[2], period_top, period_cols[2], period_bottom)
    pdf.setFont("Helvetica-Bold", 7.7)
    pdf.drawString(period_cols[0] + 7, period_top - 15, f"Periodo Facturado Desde: {_format_date_display(invoice.cbte_fch)}")
    pdf.drawString(period_cols[1] + 7, period_top - 15, f"Hasta: {_format_date_display(invoice.cbte_fch)}")
    pdf.drawString(period_cols[2] + 7, period_top - 15, f"Fecha de Vto. para el pago: {_format_date_display(invoice.cbte_fch)}")

    customer_h = 24 * mm
    customer_top = period_bottom
    customer_bottom = customer_top - customer_h
    pdf.rect(left, customer_bottom, right - left, customer_h, stroke=1, fill=0)
    _draw_field(pdf, left + 8, customer_top - 16, f"{_doc_label(invoice.doc_tipo)}:", str(patient_document or "-"), label_w=18 * mm, value_max=72)
    _draw_field(pdf, left + 74 * mm, customer_top - 16, "Apellido y Nombre / Razon Social:", patient_name or "-", label_w=58 * mm, value_max=118)
    _draw_field(pdf, left + 8, customer_top - 35, "Condicion frente al IVA:", receptor_iva, label_w=42 * mm, value_max=85)
    _draw_field(pdf, left + 95 * mm, customer_top - 35, "Domicilio:", "-", label_w=18 * mm, value_max=88)
    _draw_field(pdf, left + 8, customer_top - 54, "Condicion de venta:", "Contado", label_w=34 * mm, value_max=80)
    if _consumer_final_legend(invoice, receptor_iva):
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawRightString(right - 8, customer_top - 54, "A CONSUMIDOR FINAL")

    table_top = customer_bottom - 3 * mm
    table_h = 79 * mm
    header_h = 12 * mm
    row_h = 22 * mm
    col_widths = [10 * mm, 63 * mm, 18 * mm, 18 * mm, 24 * mm, 16 * mm, 20 * mm, 17 * mm]
    col_titles = ["Codigo", "Producto / Servicio", "Cantidad", "U. Medida", "Precio Unit.", "% Bonif", "Imp. Bonif.", "Subtotal"]
    xs = [left]
    for col_w in col_widths:
        xs.append(xs[-1] + col_w)
    table_right = xs[-1]

    pdf.setLineWidth(0.6)
    pdf.setStrokeColor(colors.HexColor("#111111"))
    pdf.rect(left, table_top - table_h, table_right - left, table_h, stroke=1, fill=0)
    pdf.setFillColor(colors.HexColor("#d9d9d9"))
    pdf.rect(left, table_top - header_h, table_right - left, header_h, stroke=0, fill=1)
    pdf.setFillColor(colors.black)
    pdf.setLineWidth(0.5)
    pdf.line(left, table_top - header_h, table_right, table_top - header_h)
    pdf.line(left, table_top - header_h - row_h, table_right, table_top - header_h - row_h)
    for x in xs[1:-1]:
        pdf.line(x, table_top, x, table_top - table_h)

    pdf.setFont("Helvetica-Bold", 7.5)
    for index, title_text in enumerate(col_titles):
        _draw_centered_wrapped(pdf, title_text, xs[index], xs[index + 1], table_top - 12, 8, max_lines=2)

    item_y = table_top - header_h - 12
    pdf.setFont("Helvetica", 8.2)
    pdf.drawCentredString((xs[0] + xs[1]) / 2, item_y, "")
    _draw_wrapped(pdf, description, xs[1] + 5, item_y, col_widths[1] - 10, 10, max_lines=2)
    if diagnosis:
        pdf.setFont("Helvetica-Bold", 7.5)
        pdf.drawString(xs[1] + 5, item_y - 28, "Diagnostico:")
        pdf.setFont("Helvetica", 7.5)
        _draw_wrapped(pdf, diagnosis, xs[1] + 55, item_y - 28, col_widths[1] - 61, 9, max_lines=2)
    pdf.setFont("Helvetica", 8.2)
    pdf.drawRightString(xs[3] - 4, item_y, "1,00")
    pdf.drawCentredString((xs[3] + xs[4]) / 2, item_y, "unidades")
    pdf.drawRightString(xs[5] - 4, item_y, _money_ar(amount))
    pdf.drawRightString(xs[6] - 4, item_y, "0,00")
    pdf.drawRightString(xs[7] - 4, item_y, "0,00")
    pdf.drawRightString(xs[8] - 4, item_y, _money_ar(amount))

    totals_top = table_top - table_h - 4 * mm
    totals_h = 30 * mm
    totals_w = 72 * mm
    totals_left = right - totals_w
    pdf.setLineWidth(0.6)
    pdf.rect(totals_left, totals_top - totals_h, totals_w, totals_h, stroke=1, fill=0)
    pdf.setFont("Helvetica-Bold", 9)
    _draw_total_row(pdf, totals_left, right, totals_top - 17, "Subtotal:", _money_ar(invoice.imp_neto or amount))
    _draw_total_row(pdf, totals_left, right, totals_top - 38, "Importe Otros Tributos:", _money_ar(invoice.imp_trib))
    pdf.setFont("Helvetica-Bold", 11)
    _draw_total_row(pdf, totals_left, right, totals_top - 64, "Importe Total:", _money_ar(amount))

    if professional_legend:
        legend_top = totals_top - totals_h - 11 * mm
        pdf.rect(left, legend_top - 10 * mm, right - left, 10 * mm, stroke=1, fill=0)
        pdf.setFont("Helvetica-Oblique", 9)
        pdf.drawCentredString(center, legend_top - 6 * mm, _clip(f'"{professional_legend}"', 90))

    footer_y = 31 * mm
    qr_size = 26 * mm
    pdf.drawImage(qr_reader, left, footer_y, width=qr_size, height=qr_size, mask="auto")
    arca_x = left + 35 * mm
    pdf.setFont("Helvetica-Bold", 19)
    pdf.setFillColor(colors.HexColor("#444444"))
    pdf.drawString(arca_x, footer_y + 21 * mm, "ARCA")
    pdf.setFont("Helvetica", 5.2)
    pdf.drawString(arca_x + 2, footer_y + 17 * mm, "AGENCIA DE RECAUDACION")
    pdf.drawString(arca_x + 2, footer_y + 14 * mm, "Y CONTROL ADUANERO")
    pdf.setFillColor(colors.black)
    pdf.setFont("Helvetica-BoldOblique", 9)
    pdf.drawString(arca_x, footer_y + 3 * mm, "Comprobante Autorizado")
    pdf.setFont("Helvetica-BoldOblique", 6.5)
    _draw_wrapped(pdf, "Esta Agencia no se responsabiliza por los datos ingresados en el detalle de la operacion", arca_x, footer_y - 6 * mm, 62 * mm, 8, max_lines=2)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(left + 96 * mm, footer_y + 5 * mm, "Pag. 1/1")
    cae_x = right - 65 * mm
    cae_value_x = right
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(cae_x, footer_y + 20 * mm, "CAE Nro:")
    pdf.drawString(cae_x, footer_y + 10 * mm, "Fecha de Vto. de CAE:")
    pdf.setFont("Helvetica", 10)
    pdf.drawRightString(cae_value_x, footer_y + 20 * mm, str(invoice.cae or "-"))
    pdf.drawRightString(cae_value_x, footer_y + 10 * mm, _format_date_display(invoice.cae_fch_vto))


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


def _receipt_type_label(cbte_tipo: Any) -> str:
    mapping = {
        1: "Factura A",
        2: "Nota de debito A",
        3: "Nota de credito A",
        6: "Factura B",
        7: "Nota de debito B",
        8: "Nota de credito B",
        11: "Factura C",
        12: "Nota de debito C",
        13: "Nota de credito C",
    }
    try:
        return mapping.get(int(cbte_tipo or 0), f"Comprobante {cbte_tipo}")
    except (TypeError, ValueError):
        return "Comprobante"


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


def _consumer_final_legend(invoice: ArcaInvoice, receptor_iva: str) -> bool:
    try:
        cbte_tipo = int(invoice.cbte_tipo or 0)
    except (TypeError, ValueError):
        cbte_tipo = 0
    return cbte_tipo in {6, 11} and "consumidor final" in receptor_iva.lower()


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
    formatted = f"{amount:,.2f}"
    return formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def _draw_total_row(pdf: Any, left: float, right: float, y: float, label: str, amount: str) -> None:
    pdf.drawRightString(right - 72, y, label)
    pdf.drawString(right - 62, y, "$")
    pdf.drawRightString(right - 12, y, amount)


def _draw_field(
    pdf: Any,
    x: float,
    y: float,
    label: str,
    value: Any,
    *,
    label_w: float,
    value_max: int,
) -> None:
    pdf.setFont("Helvetica-Bold", 8.4)
    pdf.drawString(x, y, label)
    pdf.setFont("Helvetica", 8.4)
    pdf.drawString(x + label_w, y, _clip(value, value_max))


def _draw_centered_wrapped(
    pdf: Any,
    text: str,
    x0: float,
    x1: float,
    y: float,
    line_height: float,
    *,
    max_lines: int,
) -> None:
    max_width = max(1, x1 - x0 - 4)
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
    center = (x0 + x1) / 2
    for index, line in enumerate(lines[:max_lines]):
        pdf.drawCentredString(center, y - index * line_height, line)


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
