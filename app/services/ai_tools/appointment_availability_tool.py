from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import hashlib
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_ba
from app.integrations.consultorio_movil import CabildoConfigError
from app.models.availability_hold import AvailabilityHold, AvailabilityHoldStatus
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.services.ai_tools.base import tool_error, tool_success
from app.services.calendar_service import CalendarService
from app.services.consultorio_eligibility_service import (
    get_eligibility_config,
    has_specific_coverage_match,
    is_consultorio_eligible,
    slot_is_in_weekly_ranges,
)

logger = logging.getLogger(__name__)


async def get_available_appointment_slots(
    session: AsyncSession,
    *,
    tenant_id: int,
    consultorio_type: str,
    patient_context: dict,
    preferences: dict,
    limit: int = 5,
) -> dict[str, Any]:
    normalized_type = _normalize_consultorio_type(consultorio_type)
    safe_limit = max(1, min(int(limit or 5), 50))
    try:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return tool_error(
                consultorio_type=normalized_type,
                error="tenant_not_found",
                message="No pude consultar la agenda en este momento.",
            )
        consultorios = await _get_consultorios(session, tenant_id, normalized_type)
        coverage_type, coverage_entity = await _resolve_patient_coverage(
            session, tenant_id, patient_context
        )
        first_time = bool(
            patient_context.get("first_time")
            if patient_context.get("first_time") is not None
            else (patient_context.get("appointment") or {}).get("is_first_time")
        )
        if not consultorios:
            response = tool_error(
                consultorio_type=normalized_type,
                error="consultorio_not_configured",
                message="No hay consultorio configurado para consultar disponibilidad.",
            )
            response["availability_context"] = {
                "patient_id": patient_context.get("patient_id"),
                "modality": normalized_type,
                "first_consultation": first_time,
                "coverage": {
                    "type": coverage_type,
                    "entity": coverage_entity,
                },
                "rules_applied": [],
                "sources_consulted": [],
            }
            return response
        eligibility_evaluations: list[dict[str, Any]] = []
        eligible: list[Consultorio] = []
        for consultorio in consultorios:
            is_eligible = is_consultorio_eligible(
                consultorio,
                first_time=first_time,
                coverage_type=coverage_type,
                coverage_entity=coverage_entity,
            )
            config = get_eligibility_config(consultorio)
            eligibility_evaluations.append(
                {
                    "consultorio_id": consultorio.id,
                    "consultorio_name": consultorio.nombre,
                    "provider": CalendarService().resolve_provider_name(consultorio),
                    "eligible": is_eligible,
                    "rules": {
                        "accepts_first_consultation": config["accepts_first_consultation"],
                        "coverage_types": config["coverage_types"],
                        "coverage_entities": config["coverage_entities"],
                        "weekly_ranges": config["weekly_ranges"],
                    },
                }
            )
            if is_eligible:
                eligible.append(consultorio)
        specific_matches = [
            consultorio
            for consultorio in eligible
            if has_specific_coverage_match(consultorio, coverage_entity)
        ]
        if specific_matches:
            eligible = specific_matches
        selected_ids = {consultorio.id for consultorio in eligible}
        for evaluation in eligibility_evaluations:
            evaluation["selected"] = evaluation["consultorio_id"] in selected_ids
        availability_context: dict[str, Any] = {
            "patient_id": patient_context.get("patient_id"),
            "modality": normalized_type,
            "first_consultation": first_time,
            "coverage": {
                "type": coverage_type,
                "entity": coverage_entity,
            },
            "rules_applied": eligibility_evaluations,
            "sources_consulted": [],
        }
        if not eligible:
            response = tool_error(
                consultorio_type=normalized_type,
                error="no_eligible_consultorio",
                message="No hay consultorios elegibles para esta solicitud.",
            )
            response["availability_context"] = availability_context
            return response
        slots: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        sources: list[str] = []
        for consultorio in eligible:
            provider = CalendarService().resolve_provider_name(consultorio)
            if provider not in {"google", "drapp", "consultorio_movil"}:
                continue
            source_attempt: dict[str, Any] = {
                "consultorio_id": consultorio.id,
                "consultorio_name": consultorio.nombre,
                "provider": provider,
                "status": "consulting",
            }
            availability_context["sources_consulted"].append(source_attempt)
            consultorio_type_value = (
                "virtual" if consultorio.tipo == TipoConsultorio.VIRTUAL else "presential"
            )
            try:
                result = await _get_provider_slots(
                    tenant=tenant,
                    consultorio=consultorio,
                    consultorio_type=consultorio_type_value,
                    preferences=preferences,
                    limit=safe_limit,
                    provider=provider,
                )
            except Exception as exc:
                source_attempt["status"] = "failed"
                source_attempt["error"] = type(exc).__name__
                logger.warning(
                    "availability_source_failed tenant_id=%s consultorio_id=%s error=%s",
                    tenant.id,
                    consultorio.id,
                    type(exc).__name__,
                )
                errors.append(
                    tool_error(
                        consultorio_type=consultorio_type_value,
                        source=provider,
                        error=type(exc).__name__,
                        message="No pude consultar una de las agendas.",
                    )
                )
                continue
            if result.get("ok"):
                source_attempt["status"] = (
                    "available" if result.get("slots") else "no_availability"
                )
                slots.extend(result.get("slots") or [])
                if result.get("source"):
                    sources.append(str(result["source"]))
            else:
                source_attempt["status"] = "failed"
                source_attempt["error"] = str(result.get("error") or "provider_error")
                errors.append(result)
        active_holds_result = await session.execute(
            select(AvailabilityHold.consultorio_id, AvailabilityHold.external_slot_id).where(
                AvailabilityHold.tenant_id == tenant_id,
                AvailabilityHold.status == AvailabilityHoldStatus.ACTIVE,
                AvailabilityHold.expires_at > now_ba(),
            )
        )
        active_holds = {
            (int(consultorio_id), str(external_slot_id))
            for consultorio_id, external_slot_id in active_holds_result.all()
        }
        slots = [
            slot
            for slot in slots
            if (
                int((slot.get("metadata") or {}).get("consultorio_id") or 0),
                str((slot.get("metadata") or {}).get("external_event_id") or ""),
            )
            not in active_holds
        ]
        slots.sort(key=lambda slot: str(slot.get("start_at") or ""))
        if slots:
            response = tool_success(
                consultorio_type=normalized_type,
                source=sources[0] if sources and len(set(sources)) == 1 else "combined",
                slots=slots[:safe_limit],
                message=None,
            )
            response["partial"] = bool(errors)
            response["availability_context"] = availability_context
            return response
        if errors:
            response = errors[0]
            response["availability_context"] = availability_context
            return response
        response = tool_success(
            consultorio_type=normalized_type,
            source="combined",
            slots=[],
            message="No encontre turnos disponibles.",
        )
        response["availability_context"] = availability_context
        return response
    except Exception as exc:
        logger.warning(
            "ai_availability_tool_failed tenant_id=%s consultorio_type=%s error=%s",
            tenant_id,
            normalized_type,
            type(exc).__name__,
        )
        return tool_error(
            consultorio_type=normalized_type,
            error=type(exc).__name__,
            message="No pude consultar la agenda en este momento.",
        )


async def _get_provider_slots(
    *,
    tenant: Tenant,
    consultorio: Consultorio,
    consultorio_type: str,
    preferences: dict,
    limit: int,
    provider: str,
) -> dict[str, Any]:
    tz_name = (tenant.calendar_settings or {}).get("default_timezone") or "America/Argentina/Buenos_Aires"
    tz = _resolve_timezone(tz_name)
    start, end = _availability_window(tz, preferences)
    try:
        slots = await CalendarService().list_available_slots(tenant, consultorio, start, end)
    except CabildoConfigError:
        return tool_error(
            consultorio_type=consultorio_type,
            source=provider,
            error=f"{provider}_not_configured",
            message="La agenda presencial no esta configurada para consulta automatica.",
        )
    normalized = [
        _normalize_slot(
            tenant_id=tenant.id,
            consultorio_type=consultorio_type,
            raw_slot_id=slot.slot_id,
            start_at=slot.start_at,
            end_at=slot.end_at,
            timezone=slot.timezone or tz_name,
            provider=slot.provider or provider,
            metadata={
                "calendar_id_hash": _hash_value(slot.calendar_id),
                # La referencia autoritativa queda solamente en el estado del
                # servidor; el paciente selecciona el identificador opaco.
                "consultorio_id": consultorio.id,
                "external_event_id": slot.slot_id,
            },
        )
        for slot in slots
        if slot_is_in_weekly_ranges(consultorio, slot.start_at, slot.end_at)
    ]
    return tool_success(
        consultorio_type=consultorio_type,
        source="calendar" if provider == "google" else provider,
        slots=normalized[:limit],
        message=None if normalized else "No encontre turnos disponibles.",
    )


async def _get_consultorios(
    session: AsyncSession,
    tenant_id: int,
    consultorio_type: str,
) -> list[Consultorio]:
    filters = [Consultorio.tenant_id == tenant_id, Consultorio.deleted_at.is_(None)]
    if consultorio_type != "both":
        tipo = TipoConsultorio.VIRTUAL if consultorio_type == "virtual" else TipoConsultorio.PRESENCIAL
        filters.append(Consultorio.tipo == tipo)
    result = await session.execute(select(Consultorio).where(*filters).order_by(Consultorio.id.asc()))
    return list(result.scalars().all())


async def _resolve_patient_coverage(
    session: AsyncSession,
    tenant_id: int,
    patient_context: dict,
) -> tuple[str, str]:
    if patient_context.get("for_whom") == "other":
        entity = str(patient_context.get("other_insurance") or "").strip()
        return _infer_coverage_type(None, entity), entity
    patient = None
    try:
        patient_id = int(patient_context.get("patient_id"))
    except (TypeError, ValueError):
        patient_id = 0
    if patient_id:
        candidate = await session.get(Paciente, patient_id)
        if candidate is not None and candidate.tenant_id == tenant_id:
            patient = candidate
    entity = str(getattr(patient, "obra_social", "") or patient_context.get("insurance") or "").strip()
    explicit_type = str(getattr(patient, "health_coverage_type", "") or patient_context.get("coverage_type") or "")
    return _infer_coverage_type(explicit_type, entity), entity


def _infer_coverage_type(explicit_type: str | None, entity: str) -> str:
    normalized_type = str(explicit_type or "").strip().lower()
    if normalized_type in {"particular", "obra_social", "prepaga"}:
        return normalized_type
    normalized_entity = str(entity or "").strip().lower()
    return "particular" if normalized_entity in {"", "particular", "sin cobertura"} else "obra_social"


def _normalize_slot(
    *,
    tenant_id: int,
    consultorio_type: str,
    raw_slot_id: str,
    start_at: datetime,
    end_at: datetime,
    timezone: str,
    provider: str,
    metadata: dict[str, Any] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    start = start_at if start_at.tzinfo else start_at.replace(tzinfo=_resolve_timezone(timezone))
    end = end_at if end_at.tzinfo else end_at.replace(tzinfo=_resolve_timezone(timezone))
    safe_id = _opaque_slot_id(tenant_id, consultorio_type, provider, raw_slot_id, start.isoformat())
    return {
        "slot_id": safe_id,
        "label": label or _format_slot_label(start),
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "timezone": timezone,
        "provider": provider,
        "consultorio_type": consultorio_type,
        "metadata": metadata or {},
    }


def _availability_window(tz: tzinfo, preferences: dict) -> tuple[datetime, datetime]:
    now = now_ba().astimezone(tz)
    preferred_day = str((preferences or {}).get("preferred_day") or "").strip().lower()
    weekdays = {
        "lunes": 0,
        "martes": 1,
        "miercoles": 2,
        "miércoles": 2,
        "jueves": 3,
        "viernes": 4,
        "sabado": 5,
        "sábado": 5,
        "domingo": 6,
    }
    if preferred_day in weekdays:
        delta = (weekdays[preferred_day] - now.weekday()) % 7
        delta = delta or 7
        day = now + timedelta(days=delta)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start = now.replace(second=0, microsecond=0)
    return start, start + timedelta(days=21)


def _format_slot_label(start: datetime) -> str:
    days = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
    return f"{days[start.weekday()]} {start.strftime('%d/%m a las %H:%M')}"


def _normalize_consultorio_type(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"both", "any", "ambos", "cualquiera"}:
        return "both"
    return "virtual" if text == "virtual" else "presential"


def _resolve_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-3))


def _opaque_slot_id(*parts: str | int) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _hash_value(value: str | None) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
