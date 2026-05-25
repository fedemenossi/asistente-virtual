from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import hashlib
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_ba
from app.integrations import consultorio_movil
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.tenant import Tenant
from app.services.ai_tools.base import tool_error, tool_success
from app.services.calendar_service import CalendarService

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
    del patient_context
    normalized_type = _normalize_consultorio_type(consultorio_type)
    safe_limit = max(1, min(int(limit or 5), 10))
    try:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return tool_error(
                consultorio_type=normalized_type,
                error="tenant_not_found",
                message="No pude consultar la agenda en este momento.",
            )
        consultorio = await _get_consultorio(session, tenant_id, normalized_type)
        if consultorio is None:
            return tool_error(
                consultorio_type=normalized_type,
                error="consultorio_not_configured",
                message="No hay consultorio configurado para consultar disponibilidad.",
            )
        if normalized_type == "virtual":
            return await _get_virtual_slots(
                tenant=tenant,
                consultorio=consultorio,
                preferences=preferences,
                limit=safe_limit,
            )
        return await _get_presential_slots(
            tenant=tenant,
            consultorio=consultorio,
            limit=safe_limit,
        )
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


async def _get_virtual_slots(
    *,
    tenant: Tenant,
    consultorio: Consultorio,
    preferences: dict,
    limit: int,
) -> dict[str, Any]:
    tz_name = (tenant.calendar_settings or {}).get("default_timezone") or "America/Argentina/Buenos_Aires"
    tz = _resolve_timezone(tz_name)
    start, end = _availability_window(tz, preferences)
    slots = await CalendarService().list_available_slots(tenant, consultorio, start, end)
    normalized = [
        _normalize_slot(
            tenant_id=tenant.id,
            consultorio_type="virtual",
            raw_slot_id=slot.slot_id,
            start_at=slot.start_at,
            end_at=slot.end_at,
            timezone=slot.timezone or tz_name,
            provider=slot.provider or "google_calendar",
            metadata={"calendar_id_hash": _hash_value(slot.calendar_id)},
        )
        for slot in slots[:limit]
    ]
    return tool_success(
        consultorio_type="virtual",
        source="calendar",
        slots=normalized,
        message=None if normalized else "No encontre turnos virtuales disponibles.",
    )


async def _get_presential_slots(
    *,
    tenant: Tenant,
    consultorio: Consultorio,
    limit: int,
) -> dict[str, Any]:
    try:
        selections = consultorio_movil.list_next_presential_slots(
            tenant=tenant,
            consultorio=consultorio,
            limit=limit,
        )
    except consultorio_movil.CabildoConfigError:
        return tool_error(
            consultorio_type="presential",
            source="consultorio_movil",
            error="consultorio_movil_not_configured",
            message="La agenda presencial no esta configurada para consulta automatica.",
        )
    normalized = [
        _normalize_slot(
            tenant_id=tenant.id,
            consultorio_type="presential",
            raw_slot_id=f"{selection.number}:{selection.start_at.isoformat()}",
            start_at=selection.start_at,
            end_at=selection.end_at,
            timezone=selection.timezone,
            provider="consultorio_movil",
            metadata={"duration_minutes": selection.duration_minutes},
            label=selection.label,
        )
        for selection in selections[:limit]
    ]
    return tool_success(
        consultorio_type="presential",
        source="consultorio_movil",
        slots=normalized,
        message=None if normalized else "No encontre turnos presenciales disponibles.",
    )


async def _get_consultorio(
    session: AsyncSession,
    tenant_id: int,
    consultorio_type: str,
) -> Consultorio | None:
    tipo = TipoConsultorio.VIRTUAL if consultorio_type == "virtual" else TipoConsultorio.PRESENCIAL
    result = await session.execute(
        select(Consultorio)
        .where(
            Consultorio.tenant_id == tenant_id,
            Consultorio.tipo == tipo,
            Consultorio.deleted_at.is_(None),
        )
        .order_by(Consultorio.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
