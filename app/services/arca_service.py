from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import anyio
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.arca.config import ArcaWsSettings
from app.integrations.arca.wsaa_client import AccessTicket, WsaaClient, WsaaError
from app.integrations.arca.wsfe_client import WsfeClient, WsfeError
from app.models.arca_billable_item import ArcaBillableItem
from app.models.arca_invoice import ArcaInvoice, ArcaInvoiceStatus
from app.models.arca_invoice_event import ArcaInvoiceEvent
from app.models.billing_external_consultation import BillingExternalConsultation
from app.models.billing_invoice_line import BillingInvoiceLine
from app.models.paciente import Paciente
from app.models.billing_fiscal_contact import BillingFiscalContact
from app.models.tenant import Tenant
from app.repositories.arca_ticket_repository import ArcaTicketRepository
from app.services.billing_arca_settings_service import decrypt_secret


logger = logging.getLogger(__name__)


class ArcaConfigurationError(RuntimeError):
    pass


class ArcaConnectivityError(RuntimeError):
    pass


class ArcaEmissionError(RuntimeError):
    pass


class ArcaInvoiceAlreadyExists(RuntimeError):
    pass


@dataclass(frozen=True)
class ArcaConnectionTestResult:
    environment: str
    represented_cuit: str
    dummy: dict[str, Any]
    points_of_sale: list[dict[str, Any]]
    used_cached_ticket: bool


@dataclass(frozen=True)
class ArcaEmissionResult:
    invoice: ArcaInvoice
    recovered: bool = False


class ArcaService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        wsaa_factory: Callable[[ArcaWsSettings], WsaaClient] = WsaaClient,
        wsfe_factory: Callable[[ArcaWsSettings, Callable[[], dict[str, Any]]], WsfeClient] = WsfeClient,
    ) -> None:
        self._session = session
        self._ticket_repo = ArcaTicketRepository(session)
        self._wsaa_factory = wsaa_factory
        self._wsfe_factory = wsfe_factory
        self._last_used_cached_ticket = False

    def build_settings(self, tenant: Tenant) -> ArcaWsSettings:
        data = tenant.arca_settings or {}
        if not data.get("enabled"):
            raise ArcaConfigurationError("La configuracion ARCA no esta habilitada.")
        represented_cuit = str(data.get("represented_cuit") or "").strip()
        environment = str(data.get("environment") or "homo").strip().lower()
        service = str(data.get("service") or "wsfe").strip() or "wsfe"
        cert_pem = decrypt_secret(data.get("certificate_encrypted"))
        key_pem = decrypt_secret(data.get("private_key_encrypted"))
        key_passphrase = decrypt_secret(data.get("key_passphrase_encrypted")) or None
        if environment not in {"homo", "prod"}:
            raise ArcaConfigurationError("Ambiente ARCA invalido.")
        if not represented_cuit.isdigit() or len(represented_cuit) != 11:
            raise ArcaConfigurationError("CUIT ARCA invalido.")
        if not cert_pem:
            raise ArcaConfigurationError("Certificado ARCA no configurado.")
        if not key_pem:
            raise ArcaConfigurationError("Clave privada ARCA no configurada.")
        return ArcaWsSettings(
            environment=environment,
            represented_cuit=int(represented_cuit),
            service=service,
            cert_pem=cert_pem,
            key_pem=key_pem,
            key_passphrase=key_passphrase,
        )

    async def get_ticket(self, tenant: Tenant, settings: ArcaWsSettings) -> AccessTicket:
        represented_cuit = str(settings.represented_cuit)
        cached = await self._ticket_repo.load_ticket(
            tenant.id,
            represented_cuit,
            settings.environment,
            settings.service,
        )
        margin = datetime.now(timezone.utc) + timedelta(minutes=5)
        if cached:
            expiration = cached.expiration_time
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            if expiration > margin:
                self._last_used_cached_ticket = True
                return cached

        self._last_used_cached_ticket = False
        try:
            ticket = await anyio.to_thread.run_sync(
                self._wsaa_factory(settings).request_ticket
            )
        except WsaaError as exc:
            raise ArcaConnectivityError(str(exc)) from exc
        await self._ticket_repo.save_ticket(
            tenant.id,
            represented_cuit,
            settings.environment,
            settings.service,
            ticket,
        )
        return ticket

    async def test_connection(self, tenant: Tenant) -> ArcaConnectionTestResult:
        settings = self.build_settings(tenant)
        ticket = await self.get_ticket(tenant, settings)

        def auth_provider() -> dict[str, Any]:
            return {
                "Token": ticket.token,
                "Sign": ticket.sign,
                "Cuit": settings.represented_cuit,
            }

        wsfe = self._wsfe_factory(settings, auth_provider)
        try:
            dummy = await anyio.to_thread.run_sync(lambda: wsfe.dummy().data)
            points = await anyio.to_thread.run_sync(lambda: wsfe.get_puntos_venta().data)
        except WsfeError as exc:
            raise ArcaConnectivityError(str(exc)) from exc

        normalized_points: list[dict[str, Any]] = []
        if isinstance(points, dict):
            raw_points = points.get("PtoVenta") or points.get("ptoVenta") or []
            if isinstance(raw_points, dict):
                raw_points = [raw_points]
            normalized_points = [item for item in raw_points if isinstance(item, dict)]
        elif isinstance(points, list):
            normalized_points = [item for item in points if isinstance(item, dict)]

        return ArcaConnectionTestResult(
            environment=settings.environment,
            represented_cuit=str(settings.represented_cuit),
            dummy=dummy if isinstance(dummy, dict) else {"result": dummy},
            points_of_sale=normalized_points,
            used_cached_ticket=self._last_used_cached_ticket,
        )

    async def emit_invoice_for_consultation(
        self,
        tenant: Tenant,
        consultation: BillingExternalConsultation,
        item: ArcaBillableItem,
        *,
        amount_override: Any | None = None,
        send_email: bool | None = None,
    ) -> ArcaEmissionResult:
        if consultation.tenant_id != tenant.id or item.tenant_id != tenant.id:
            raise ArcaEmissionError("La consulta o el item no pertenecen al tenant.")
        await self._session.refresh(consultation, with_for_update=True)
        if consultation.arca_invoice_id is not None:
            raise ArcaInvoiceAlreadyExists("La consulta ya esta facturada.")
        existing_authorized = await self._session.scalar(
            select(ArcaInvoice.id).where(
                ArcaInvoice.tenant_id == tenant.id,
                ArcaInvoice.external_consultation_id == consultation.id,
                ArcaInvoice.status == ArcaInvoiceStatus.AUTHORIZED,
            )
        )
        if existing_authorized is not None:
            raise ArcaInvoiceAlreadyExists("La consulta ya tiene una factura autorizada.")
        diagnosis = (consultation.diagnosis or "").strip()
        if not item.active:
            raise ArcaEmissionError("El item facturable esta inactivo.")

        settings = self.build_settings(tenant)
        ticket = await self.get_ticket(tenant, settings)

        def auth_provider() -> dict[str, Any]:
            return {
                "Token": ticket.token,
                "Sign": ticket.sign,
                "Cuit": settings.represented_cuit,
            }

        wsfe = self._wsfe_factory(settings, auth_provider)
        arca_cfg = tenant.arca_settings or {}
        pto_vta = int(arca_cfg.get("default_pto_vta") or 0)
        cbte_tipo = int(arca_cfg.get("default_cbte_tipo") or 11)
        concepto = int(item.concepto or arca_cfg.get("default_concepto") or 2)
        if pto_vta <= 0:
            raise ArcaEmissionError("Punto de venta ARCA invalido.")

        try:
            latest = await anyio.to_thread.run_sync(
                lambda: wsfe.get_ultimo_autorizado(pto_vta, cbte_tipo).data
            )
        except WsfeError as exc:
            raise ArcaConnectivityError(str(exc)) from exc
        last_number = _extract_int(latest, "CbteNro", 0)
        cbte_nro = last_number + 1
        line_description, insurance_name, insurance_number = await self._billing_line_description(
            tenant.id,
            consultation,
            item,
        )
        request = self._build_fe_cae_request(
            tenant=tenant,
            consultation=consultation,
            item=item,
            amount_override=amount_override,
            pto_vta=pto_vta,
            cbte_tipo=cbte_tipo,
            cbte_nro=cbte_nro,
            concepto=concepto,
            line_description=line_description,
            insurance_name=insurance_name,
            insurance_number=insurance_number,
        )
        invoice = self._build_invoice(
            tenant=tenant,
            consultation=consultation,
            item=item,
            request=request,
            send_email=send_email,
            pto_vta=pto_vta,
            cbte_tipo=cbte_tipo,
            cbte_nro=cbte_nro,
            concepto=concepto,
            settings=settings,
        )
        self._session.add(invoice)
        await self._session.flush()
        self._session.add(
            ArcaInvoiceEvent(
                invoice_id=invoice.id,
                event_type="authorization_requested",
                payload_json={"consultation_id": consultation.id},
            )
        )
        self._session.add(
            _build_invoice_line(
                invoice.id,
                item,
                diagnosis,
                amount_override=amount_override,
                description=line_description,
            )
        )

        try:
            response = await anyio.to_thread.run_sync(lambda: wsfe.solicitar_cae(_soap_fe_cae_request(request)).data)
        except WsfeError as exc:
            recovered = await self._recover_invoice(wsfe, invoice, pto_vta, cbte_tipo, cbte_nro)
            if recovered:
                consultation.arca_invoice_id = invoice.id
                consultation.status = "billed"
                consultation.billed_at = datetime.now()
                return ArcaEmissionResult(invoice=invoice, recovered=True)
            invoice.status = ArcaInvoiceStatus.REJECTED
            invoice.error_message = str(exc)
            consultation.status = "error"
            self._session.add(
                ArcaInvoiceEvent(
                    invoice_id=invoice.id,
                    event_type="authorization_rejected",
                    payload_json={"error": str(exc)},
                )
            )
            raise ArcaEmissionError(str(exc)) from exc
        self._apply_authorization_response(invoice, response)
        if invoice.status != ArcaInvoiceStatus.AUTHORIZED:
            self._session.add(
                ArcaInvoiceEvent(
                    invoice_id=invoice.id,
                    event_type="authorization_rejected",
                    payload_json={"result": "rejected", "error": invoice.error_message},
                )
            )
            raise ArcaEmissionError(invoice.error_message or "ARCA rechazo la autorizacion.")
        consultation.arca_invoice_id = invoice.id
        consultation.status = "billed"
        consultation.billed_at = datetime.now()
        self._session.add(
            ArcaInvoiceEvent(
                invoice_id=invoice.id,
                event_type="authorization_approved",
                payload_json={"cae": invoice.cae, "cbte_nro": invoice.cbte_nro},
            )
        )
        return ArcaEmissionResult(invoice=invoice, recovered=False)

    async def emit_manual_invoice_for_patient(
        self, tenant: Tenant, patient: Paciente, item: ArcaBillableItem, *, amount: Any,
        service_start, service_end, sale_condition: str, send_email: bool,
    ) -> ArcaEmissionResult:
        if patient.tenant_id != tenant.id or item.tenant_id != tenant.id or patient.deleted_at is not None:
            raise ArcaEmissionError("El paciente o el item no pertenecen al tenant.")
        if not item.active or item.currency != "PES" or int(item.concepto) != 2:
            raise ArcaEmissionError("La factura manual requiere un item activo de servicios en pesos.")
        if not patient.iva_condition:
            raise ArcaEmissionError("El paciente no tiene condicion frente al IVA.")
        settings = self.build_settings(tenant)
        if settings.environment != "prod":
            raise ArcaConfigurationError("La factura manual solo se emite con configuracion ARCA de produccion.")
        ticket = await self.get_ticket(tenant, settings)
        def auth_provider() -> dict[str, Any]:
            return {"Token": ticket.token, "Sign": ticket.sign, "Cuit": settings.represented_cuit}
        pto_vta = int((tenant.arca_settings or {}).get("default_pto_vta") or 0)
        if pto_vta <= 0:
            raise ArcaEmissionError("Punto de venta ARCA invalido.")
        wsfe = self._wsfe_factory(settings, auth_provider)
        try:
            latest = await anyio.to_thread.run_sync(lambda: wsfe.get_ultimo_autorizado(pto_vta, 11).data)
        except WsfeError as exc:
            raise ArcaConnectivityError(str(exc)) from exc
        cbte_nro = _extract_int(latest, "CbteNro", 0) + 1
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
        if value <= 0:
            raise ArcaEmissionError("El importe debe ser mayor a cero.")
        doc_type, doc_number = _document_for_arca(patient.numero_documento or patient.dni)
        today = datetime.now().date()
        description = (item.description or item.name).strip()
        detail = {"Concepto": 2, "DocTipo": doc_type, "DocNro": doc_number, "CbteDesde": cbte_nro, "CbteHasta": cbte_nro, "CbteFch": today.strftime("%Y%m%d"), "ImpTotal": float(value), "ImpTotConc": 0, "ImpNeto": float(value), "ImpOpEx": 0, "ImpTrib": 0, "ImpIVA": 0, "MonId": "PES", "MonCotiz": 1, "CondicionIVAReceptorId": _receiver_tax_condition_id(patient.iva_condition), "FchServDesde": service_start.strftime("%Y%m%d"), "FchServHasta": service_end.strftime("%Y%m%d"), "FchVtoPago": today.strftime("%Y%m%d")}
        request = {"FeCabReq": {"CantReg": 1, "PtoVta": pto_vta, "CbteTipo": 11}, "FeDetReq": {"FECAEDetRequest": [detail]}, "metadata": {"origin": "manual", "patient_id": patient.id, "receiver_name": f"{patient.nombre} {patient.apellido}".strip(), "description": description, "sale_condition": sale_condition, "service_period_start": service_start.isoformat(), "service_period_end": service_end.isoformat()}}
        invoice = ArcaInvoice(tenant_id=tenant.id, patient_id=patient.id, billing_item_id=item.id, origin="manual", receiver_name_snapshot=f"{patient.nombre} {patient.apellido}".strip(), receiver_iva_condition_snapshot=patient.iva_condition, service_period_start=service_start, service_period_end=service_end, sale_condition=sale_condition, represented_cuit=str(settings.represented_cuit), environment=settings.environment, pto_vta=pto_vta, cbte_tipo=11, cbte_nro=cbte_nro, concepto=2, doc_tipo=doc_type, doc_nro=str(doc_number), cbte_fch=today, imp_total=value, imp_tot_conc=Decimal("0"), imp_neto=value, imp_op_ex=Decimal("0"), imp_trib=Decimal("0"), imp_iva=Decimal("0"), mon_id="PES", mon_cotiz=Decimal("1"), status=ArcaInvoiceStatus.PENDING_AUTHORIZATION, diagnosis_original_snapshot=None, diagnosis_final_snapshot=None, send_email=send_email, email_to=patient.email, request_json=request)
        self._session.add(invoice); await self._session.flush()
        self._session.add(ArcaInvoiceEvent(invoice_id=invoice.id, event_type="authorization_requested", payload_json={"origin": "manual"}))
        self._session.add(BillingInvoiceLine(invoice_id=invoice.id, item_code=item.code, description=description, diagnosis_text="", quantity=1, unit_price=value, subtotal=value, tax_rate=item.tax_rate, total=value))
        try:
            response = await anyio.to_thread.run_sync(lambda: wsfe.solicitar_cae(_soap_fe_cae_request(request)).data)
        except WsfeError as exc:
            invoice.status = ArcaInvoiceStatus.REJECTED; invoice.error_message = str(exc)
            raise ArcaEmissionError(str(exc)) from exc
        self._apply_authorization_response(invoice, response)
        if invoice.status != ArcaInvoiceStatus.AUTHORIZED:
            raise ArcaEmissionError(invoice.error_message or "ARCA rechazo la autorizacion.")
        self._session.add(ArcaInvoiceEvent(invoice_id=invoice.id, event_type="authorization_approved", payload_json={"cae": invoice.cae, "origin": "manual"}))
        return ArcaEmissionResult(invoice=invoice)

    async def emit_manual_invoice_for_contact(
        self, tenant: Tenant, contact: BillingFiscalContact, item: ArcaBillableItem, *, amount: Any,
        service_start, service_end, sale_condition: str, send_email: bool,
    ) -> ArcaEmissionResult:
        if contact.tenant_id != tenant.id or not contact.active:
            raise ArcaEmissionError("El contacto fiscal no pertenece al tenant o esta inactivo.")
        proxy = Paciente(tenant_id=tenant.id, nombre=contact.name, apellido="", telefono="", dni=contact.document_number, email=contact.email or "", iva_condition=contact.iva_condition, numero_documento=contact.document_number)
        result = await self.emit_manual_invoice_for_patient(tenant, proxy, item, amount=amount, service_start=service_start, service_end=service_end, sale_condition=sale_condition, send_email=send_email)
        result.invoice.patient_id = None
        result.invoice.fiscal_contact_id = contact.id
        result.invoice.receiver_name_snapshot = contact.name
        result.invoice.email_to = contact.email
        return result

    async def _ensure_invoice_document(
        self,
        tenant: Tenant,
        invoice: ArcaInvoice,
        consultation: BillingExternalConsultation,
    ) -> None:
        try:
            from app.services.billing_invoice_document_service import BillingInvoiceDocumentService

            await BillingInvoiceDocumentService(self._session).ensure_document(
                tenant,
                invoice,
                consultation=consultation,
            )
        except Exception as exc:
            logger.warning(
                "billing_invoice_document_generation_failed invoice_id=%s error_type=%s message=%s",
                invoice.id,
                type(exc).__name__,
                str(exc),
            )
            self._session.add(
                ArcaInvoiceEvent(
                    invoice_id=invoice.id,
                    event_type="document_generation_failed",
                    payload_json={"error": str(exc)},
                )
            )

    def _build_fe_cae_request(
        self,
        *,
        tenant: Tenant,
        consultation: BillingExternalConsultation,
        item: ArcaBillableItem,
        amount_override: Any | None,
        pto_vta: int,
        cbte_tipo: int,
        cbte_nro: int,
        concepto: int,
        line_description: str,
        insurance_name: str,
        insurance_number: str,
    ) -> dict[str, Any]:
        doc_tipo, doc_nro = _document_for_arca(consultation.patient_document)
        amount = Decimal(str(amount_override if amount_override not in (None, "") else item.unit_price)).quantize(Decimal("0.01"))
        today = datetime.now().date()
        service_date = (consultation.attended_at or datetime.now()).date()
        diagnosis = (consultation.diagnosis or "").strip()
        sale_condition = _sale_condition(consultation.sale_condition)
        arca_cfg = tenant.arca_settings or {}
        receiver_tax_condition_id = _receiver_tax_condition_id(arca_cfg.get("receiver_tax_condition"))
        detail = {
            "Concepto": concepto,
            "DocTipo": doc_tipo,
            "DocNro": doc_nro,
            "CbteDesde": cbte_nro,
            "CbteHasta": cbte_nro,
            "CbteFch": today.strftime("%Y%m%d"),
            "ImpTotal": float(amount),
            "ImpTotConc": 0,
            "ImpNeto": float(amount),
            "ImpOpEx": 0,
            "ImpTrib": 0,
            "ImpIVA": 0,
            "MonId": item.currency,
            "MonCotiz": 1,
            "CondicionIVAReceptorId": receiver_tax_condition_id,
        }
        if concepto in {2, 3}:
            detail["FchServDesde"] = service_date.strftime("%Y%m%d")
            detail["FchServHasta"] = service_date.strftime("%Y%m%d")
            detail["FchVtoPago"] = today.strftime("%Y%m%d")
        return {
            "FeCabReq": {
                "CantReg": 1,
                "PtoVta": pto_vta,
                "CbteTipo": cbte_tipo,
            },
            "FeDetReq": {
                "FECAEDetRequest": [detail],
            },
            "metadata": {
                "tenant_id": tenant.id,
                "external_consultation_id": consultation.id,
                "external_id": consultation.external_id,
                "billable_item_id": item.id,
                "diagnosis": diagnosis,
                "description": line_description,
                "insurance_name": insurance_name,
                "insurance_number": insurance_number,
                "sale_condition": sale_condition,
                "receiver_tax_condition_id": receiver_tax_condition_id,
            },
        }

    async def _billing_line_description(
        self,
        tenant_id: int,
        consultation: BillingExternalConsultation,
        item: ArcaBillableItem,
    ) -> tuple[str, str, str]:
        patient = await self._consultation_patient(tenant_id, consultation)
        insurance_name = (
            consultation.insurance_name
            or (patient.obra_social if patient else None)
            or (patient.financiador_seguro if patient else None)
            or ""
        )
        insurance_number = (patient.insurance_number if patient else None) or ""
        description = _compose_invoice_line_description(item.name, insurance_name, insurance_number)
        return description, insurance_name.strip(), insurance_number.strip()

    async def _consultation_patient(
        self,
        tenant_id: int,
        consultation: BillingExternalConsultation,
    ) -> Paciente | None:
        if consultation.patient_id:
            patient = await self._session.get(Paciente, consultation.patient_id)
            if patient and patient.tenant_id == tenant_id:
                return patient
        document = (consultation.patient_document or "").strip()
        if not document:
            return None
        document_key = _document_key(document)
        return await self._session.scalar(
            select(Paciente).where(
                Paciente.tenant_id == tenant_id,
                Paciente.deleted_at.is_(None),
                or_(
                    Paciente.dni == document,
                    Paciente.numero_documento == document,
                    Paciente.document_number_normalized == document_key,
                ),
            )
        )

    def _build_invoice(
        self,
        *,
        tenant: Tenant,
        consultation: BillingExternalConsultation,
        item: ArcaBillableItem,
        request: dict[str, Any],
        send_email: bool | None,
        pto_vta: int,
        cbte_tipo: int,
        cbte_nro: int,
        concepto: int,
        settings: ArcaWsSettings,
    ) -> ArcaInvoice:
        detail = request["FeDetReq"]["FECAEDetRequest"][0]
        return ArcaInvoice(
            tenant_id=tenant.id,
            external_consultation_id=consultation.id,
            billing_item_id=item.id,
            represented_cuit=str(settings.represented_cuit),
            environment=settings.environment,
            pto_vta=pto_vta,
            cbte_tipo=cbte_tipo,
            cbte_nro=cbte_nro,
            concepto=concepto,
            doc_tipo=detail["DocTipo"],
            doc_nro=str(detail["DocNro"]),
            cbte_fch=datetime.strptime(detail["CbteFch"], "%Y%m%d").date(),
            imp_total=Decimal(str(detail["ImpTotal"])),
            imp_tot_conc=Decimal("0.00"),
            imp_neto=Decimal(str(detail["ImpNeto"])),
            imp_op_ex=Decimal("0.00"),
            imp_trib=Decimal("0.00"),
            imp_iva=Decimal("0.00"),
            mon_id=item.currency,
            mon_cotiz=Decimal("1.000000"),
            status=ArcaInvoiceStatus.PENDING_AUTHORIZATION,
            diagnosis_original_snapshot=(consultation.diagnosis_original or consultation.diagnosis or "").strip(),
            diagnosis_final_snapshot=(consultation.diagnosis or "").strip(),
            send_email=bool(send_email),
            email_to=consultation.patient_email,
            request_json=request,
        )

    def _apply_authorization_response(self, invoice: ArcaInvoice, response: Any) -> None:
        data = response if isinstance(response, dict) else {"response": response}
        det = _first_detail_response(data)
        cae = str(_get_any(det, "CAE", "Cae", "CodAutorizacion") or "")
        cae_vto = str(_get_any(det, "CAEFchVto", "FchVto") or "")
        result = str(_get_any(det, "Resultado") or _get_any(data.get("FeCabResp", {}) if isinstance(data, dict) else {}, "Resultado") or "")
        invoice.response_json = data
        invoice.cae = cae or None
        invoice.cae_fch_vto = _parse_arca_date(cae_vto)
        invoice.authorized_at = datetime.now()
        invoice.status = ArcaInvoiceStatus.AUTHORIZED if cae and result != "R" else ArcaInvoiceStatus.REJECTED
        if invoice.status == ArcaInvoiceStatus.REJECTED:
            invoice.error_message = _arca_rejection_message(data, det)

    async def _recover_invoice(
        self,
        wsfe: Any,
        invoice: ArcaInvoice,
        pto_vta: int,
        cbte_tipo: int,
        cbte_nro: int,
    ) -> bool:
        try:
            response = await anyio.to_thread.run_sync(
                lambda: wsfe.consultar_comprobante(pto_vta, cbte_tipo, cbte_nro).data
            )
        except WsfeError:
            return False
        data = response if isinstance(response, dict) else {"response": response}
        cae = str(_get_any(data, "CodAutorizacion", "CAE") or "")
        if not cae:
            return False
        invoice.response_json = data
        invoice.cae = cae
        invoice.cae_fch_vto = _parse_arca_date(str(_get_any(data, "FchVto") or ""))
        invoice.authorized_at = datetime.now()
        invoice.status = ArcaInvoiceStatus.AUTHORIZED
        self._session.add(
            ArcaInvoiceEvent(
                invoice_id=invoice.id,
                event_type="authorization_recovered",
                payload_json={"cae": invoice.cae, "cbte_nro": cbte_nro},
            )
        )
        return True


def _extract_int(data: Any, key: str, default: int = 0) -> int:
    if isinstance(data, dict):
        value = data.get(key, data.get(key[:1].lower() + key[1:], default))
    else:
        value = getattr(data, key, default)
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _soap_fe_cae_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "FeCabReq": request["FeCabReq"],
        "FeDetReq": request["FeDetReq"],
    }


def _document_for_arca(document: str | None) -> tuple[int, int]:
    digits = "".join(ch for ch in str(document or "") if ch.isdigit())
    if len(digits) == 11:
        return 80, int(digits)
    if digits:
        return 96, int(digits)
    return 99, 0


def _receiver_tax_condition_id(value: Any) -> int:
    text = str(value or "").strip().lower()
    if text.isdigit():
        return int(text)
    aliases = {
        "responsable inscripto": 1,
        "iva responsable inscripto": 1,
        "exento": 4,
        "iva sujeto exento": 4,
        "consumidor final": 5,
        "final": 5,
        "monotributo": 6,
        "responsable monotributo": 6,
        "monotributista": 6,
        "sujeto no categorizado": 7,
        "no categorizado": 7,
        "iva no alcanzado": 15,
        "no alcanzado": 15,
    }
    return aliases.get(text, 5)


def _get_any(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
        alt = key[:1].lower() + key[1:]
        if alt in data and data[alt] not in (None, ""):
            return data[alt]
    return None


def _first_detail_response(data: dict[str, Any]) -> dict[str, Any]:
    det = data.get("FeDetResp") or data.get("feDetResp") or data
    if isinstance(det, dict):
        det = (
            det.get("FEDetResponse")
            or det.get("feDetResponse")
            or det.get("FECAEDetResponse")
            or det.get("feCAEDetResponse")
            or det
        )
    if isinstance(det, dict):
        det = (
            det.get("FEDetResponse")
            or det.get("feDetResponse")
            or det.get("FECAEDetResponse")
            or det.get("feCAEDetResponse")
            or det
        )
    if isinstance(det, list):
        det = det[0] if det else {}
    return det if isinstance(det, dict) else {}


def _arca_rejection_message(data: dict[str, Any], detail: dict[str, Any]) -> str:
    messages: list[str] = []
    for container in (
        detail.get("Obs"),
        detail.get("obs"),
        detail.get("Observaciones"),
        detail.get("observaciones"),
        data.get("Errors"),
        data.get("errors"),
        data.get("Events"),
        data.get("events"),
    ):
        messages.extend(_arca_message_items(container))
    unique = list(dict.fromkeys(item for item in messages if item))
    if unique:
        return "ARCA rechazo la autorizacion: " + "; ".join(unique)
    return "ARCA rechazo la autorizacion."


def _arca_message_items(container: Any) -> list[str]:
    if not container:
        return []
    if isinstance(container, list):
        messages: list[str] = []
        for item in container:
            messages.extend(_arca_message_items(item))
        return messages
    if not isinstance(container, dict):
        return []

    code = _get_any(container, "Code", "codigo", "Codigo")
    msg = _get_any(container, "Msg", "mensaje", "Mensaje")
    if code or msg:
        return [f"{code}: {msg}".strip(": ")]

    messages = []
    for key in ("Obs", "obs", "Observaciones", "observaciones", "Err", "err", "Evt", "evt"):
        messages.extend(_arca_message_items(container.get(key)))
    return messages


def _parse_arca_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _build_invoice_line(
    invoice_id: int,
    item: ArcaBillableItem,
    diagnosis: str,
    *,
    amount_override: Any | None = None,
    description: str | None = None,
) -> BillingInvoiceLine:
    amount = Decimal(str(amount_override if amount_override not in (None, "") else item.unit_price)).quantize(Decimal("0.01"))
    return BillingInvoiceLine(
        invoice_id=invoice_id,
        item_code=item.code,
        description=description or item.description or item.name,
        diagnosis_text=diagnosis,
        quantity=Decimal("1.00"),
        unit_price=amount,
        subtotal=amount,
        tax_rate=item.tax_rate,
        total=amount,
    )


def _compose_invoice_line_description(item_name: str, insurance_name: str | None, insurance_number: str | None) -> str:
    parts = [(item_name or "Consulta").strip()]
    insurance = (insurance_name or "").strip()
    affiliate = (insurance_number or "").strip()
    if insurance:
        parts.append(insurance)
    if affiliate:
        parts.append(f"Afiliado {affiliate}")
    return " - ".join(part for part in parts if part)


def _document_key(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def _sale_condition(value: str | None) -> str:
    allowed = ("Contado", "Transferencia", "Otros medios")
    raw_values = [part.strip() for part in re.split(r"[/,+]", str(value or "")) if part.strip()]
    selected = [option for option in allowed if option in raw_values]
    return " / ".join(selected) if selected else "Contado"
