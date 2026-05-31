from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone, tzinfo
import secrets
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import re
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.csrf import validate_csrf
from app.core.db import get_async_session
from app.core.notifications import mark_notification_read
from app.core.security import CurrentUser, require_permission, require_tenant_admin
from app.core.templates import base_context, templates
from app.core.timezone import now_ba
from app.core.ui import add_flash
from app.core.tenancy import get_entity_or_404, get_tenant_entity_or_404
from app.models.audit_log import AuditLog
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.conversation_history import ConversationHistory
from app.models.conversacion import EstadoConversacion
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, Turno
from app.models.notification import Notification
from app.models.payment import Payment, PaymentStatus
from app.models.payment_event import PaymentEvent
from app.repositories.conversacion_repository import ConversacionRepository
from app.integrations.consultorio_movil import CabildoConfigError, list_next_presential_slots
from app.services.appointment_service import AppointmentService
from app.services.conversation_intents import (
    CATEGORY_LABELS,
    ConversationCategory,
    SUBTYPE_LABELS,
)
from app.services.ai_conversation_summary_service import (
    get_ai_summary_from_context,
    sanitize_context_for_display,
)
from app.services.messaging_service import MessagingService
from app.services.calendar_service import CalendarService
from app.services.google_calendar_slots_service import (
    WEEKDAY_KEYS,
    WEEKDAY_LABELS,
    build_google_calendar_config_from_form,
    calculate_slots,
    default_google_calendar_config,
    get_google_calendar_config,
    normalize_provider,
)
from app.services.holiday_service import HolidayService


logger = logging.getLogger(__name__)


def _template(request: Request, name: str, context: dict) -> Response:
    base = base_context(request)
    base.update(context)
    return templates.TemplateResponse(request, name, base)


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _validate_digits(value: str | None, field_name: str, errors: dict[str, str]) -> str | None:
    cleaned = _strip_optional(value)
    if cleaned and not re.fullmatch(r"[0-9]+", cleaned):
        errors[field_name] = "Solo se permiten numeros."
    return cleaned


def _mask_calendar_id(calendar_id: str | None) -> str:
    value = str(calendar_id or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def _calendar_generation_log_result(result: dict | None) -> dict:
    result = result or {}
    return {
        "calendar_id": _mask_calendar_id(result.get("calendar_id")),
        "calculated": result.get("calculated"),
        "created": result.get("created"),
        "duplicates": result.get("duplicates"),
        "conflicts": result.get("conflicts"),
        "errors": len(result.get("errors") or []),
        "first_error": (result.get("errors") or [None])[0],
    }


def _collect_tenant_profile_changes(tenant: Tenant, updates: dict[str, str | None]) -> dict:
    changes = {}
    for key, value in updates.items():
        old = getattr(tenant, key)
        if old != value:
            changes[key] = {"from": old, "to": value}
    return changes


def _build_tenant_whatsapp_webhook_url(request: Request, tenant_id: int, webhook_secret: str | None) -> str:
    if not webhook_secret:
        return ""
    settings = get_settings()
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    return f"{base}/webhook/whatsapp/{tenant_id}/{webhook_secret}"


def _ensure_webhook_secret(existing: str | None) -> str:
    value = (existing or "").strip()
    if value:
        return value
    return secrets.token_urlsafe(24)


def _selected_day(date_str: str | None) -> tuple[str, datetime, datetime]:
    if date_str:
        try:
            parsed = datetime.fromisoformat(date_str)
        except ValueError:
            parsed = now_ba()
            date_str = parsed.date().isoformat()
    else:
        parsed = now_ba()
        date_str = parsed.date().isoformat()
    start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    # SQLite stores test datetimes as naive UTC strings. Normalizing the BA day
    # bounds to UTC-naive keeps date filtering stable around midnight UTC.
    start_db = start.astimezone(timezone.utc).replace(tzinfo=None)
    end_db = end.astimezone(timezone.utc).replace(tzinfo=None)
    return date_str, start_db, end_db


def _turno_type_label(turno: Turno) -> str:
    return "Virtual" if str(turno.tipo.value if hasattr(turno.tipo, "value") else turno.tipo) == "virtual" else "Presencial"


def _turno_status_label(turno: Turno) -> tuple[str, str]:
    status = turno.status.value if turno.status is not None and hasattr(turno.status, "value") else str(turno.status or turno.estado or "")
    status = status.lower()
    if status in {"confirmed", "confirmado"}:
        return "Confirmado", "success"
    if status in {"waiting_payment"}:
        return "Esperando pago", "neutral"
    if status in {"cancelled", "cancelado"}:
        return "Cancelado", "warning"
    if status in {"completed", "completado"}:
        return "Completado", "success"
    return "Borrador", "neutral"


def _turno_provider_label(turno: Turno) -> tuple[str, str]:
    provider = (turno.provider or turno.external_calendar_provider or "manual").strip().lower()
    if provider == "google":
        return "Google", "info"
    if provider == "consultorio_movil":
        return "Consultorio Movil", "warning"
    return "Manual", "neutral"


async def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    today_str, today_start, today_end = _selected_day(None)
    pacientes_total = await session.scalar(
        select(func.count())
        .select_from(Paciente)
        .where(Paciente.tenant_id == user.tenant_id, Paciente.deleted_at.is_(None))
    )
    consultorios_total = await session.scalar(
        select(func.count())
        .select_from(Consultorio)
        .where(Consultorio.tenant_id == user.tenant_id, Consultorio.deleted_at.is_(None))
    )
    turnos_total = await session.scalar(
        select(func.count()).select_from(Turno)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(
            Turno.tenant_id == user.tenant_id,
            Consultorio.tenant_id == user.tenant_id,
            Consultorio.deleted_at.is_(None),
            Turno.deleted_at.is_(None),
        )
    )
    turnos_hoy = await session.scalar(
        select(func.count())
        .select_from(Turno)
        .where(
            Turno.tenant_id == user.tenant_id,
            Turno.deleted_at.is_(None),
            Turno.fecha_hora >= today_start,
            Turno.fecha_hora < today_end,
        )
    )
    virtuales_hoy = await session.scalar(
        select(func.count())
        .select_from(Turno)
        .where(
            Turno.tenant_id == user.tenant_id,
            Turno.deleted_at.is_(None),
            Turno.fecha_hora >= today_start,
            Turno.fecha_hora < today_end,
            Turno.tipo == "virtual",
        )
    )
    presenciales_hoy = await session.scalar(
        select(func.count())
        .select_from(Turno)
        .where(
            Turno.tenant_id == user.tenant_id,
            Turno.deleted_at.is_(None),
            Turno.fecha_hora >= today_start,
            Turno.fecha_hora < today_end,
            Turno.tipo == "presencial",
        )
    )
    conversations_result = await session.execute(
        select(EstadoConversacion).where(EstadoConversacion.tenant_id == user.tenant_id)
    )
    conversation_rows = list(conversations_result.scalars().all())
    active_cutoff = now_ba() - timedelta(minutes=30)
    conversaciones_abiertas = 0
    for row in conversation_rows:
        status = (row.status or "active").lower()
        if status == "pending":
            conversaciones_abiertas += 1
            continue
        if status == "finished":
            continue
        updated = row.updated_at
        if updated is None:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc).astimezone(active_cutoff.tzinfo)
        if updated >= active_cutoff:
            conversaciones_abiertas += 1
    conversaciones_pendientes = sum(
        1 for row in conversation_rows if (row.status or "").lower() == "pending"
    )
    todays_result = await session.execute(
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(
            Turno.tenant_id == user.tenant_id,
            Turno.deleted_at.is_(None),
            Paciente.deleted_at.is_(None),
            Consultorio.deleted_at.is_(None),
            Turno.fecha_hora >= today_start,
            Turno.fecha_hora < today_end,
        )
        .order_by(Turno.fecha_hora.asc())
    )
    todays_rows = list(todays_result.all())
    upcoming_result = await session.execute(
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(
            Turno.tenant_id == user.tenant_id,
            Turno.deleted_at.is_(None),
            Paciente.deleted_at.is_(None),
            Consultorio.deleted_at.is_(None),
            Turno.fecha_hora >= now_ba(),
        )
        .order_by(Turno.fecha_hora.asc())
        .limit(5)
    )
    upcoming_rows = list(upcoming_result.all())
    tasks = []
    for turno, paciente, consultorio in todays_rows:
        status_label, status_tone = _turno_status_label(turno)
        provider_label, provider_tone = _turno_provider_label(turno)
        requires_action = status_label in {"Borrador", "Esperando pago"} or not (turno.start_at and turno.end_at)
        tasks.append(
            {
                "turno": turno,
                "paciente": paciente,
                "consultorio": consultorio,
                "type_label": _turno_type_label(turno),
                "status_label": status_label,
                "status_tone": status_tone,
                "provider_label": provider_label,
                "provider_tone": provider_tone,
                "requires_action": requires_action,
                "has_incomplete_data": not bool(turno.start_at and turno.timezone and turno.provider),
            }
        )
    operational_summary = {
        "today_label": today_str,
        "turnos_hoy": turnos_hoy or 0,
        "virtuales_hoy": virtuales_hoy or 0,
        "presenciales_hoy": presenciales_hoy or 0,
        "conversaciones_pendientes": conversaciones_pendientes,
        "proximos_turnos": len(upcoming_rows),
    }
    return _template(
        request,
        "tenant/dashboard.html",
        {
            "kpis": {
                "pacientes": pacientes_total or 0,
                "consultorios": consultorios_total or 0,
                "turnos": turnos_total or 0,
                "conversaciones": conversaciones_abiertas,
            },
            "daily_kpis": {
                "turnos_hoy": turnos_hoy or 0,
                "proximos_turnos": len(upcoming_rows),
                "virtuales_hoy": virtuales_hoy or 0,
                "presenciales_hoy": presenciales_hoy or 0,
                "pendientes": conversaciones_pendientes,
            },
            "today_rows": todays_rows[:8],
            "upcoming_rows": upcoming_rows,
            "tasks": tasks[:8],
            "operational_summary": operational_summary,
        },
    )


async def consultorios_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("consultorio:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    result = await session.execute(
        select(Consultorio)
        .where(Consultorio.tenant_id == user.tenant_id)
        .where(Consultorio.deleted_at.is_(None))
        .order_by(Consultorio.nombre)
    )
    consultorios = list(result.scalars().all())
    return _template(
        request,
        "tenant/consultorios_list.html",
        {"consultorios": consultorios},
    )


def _consultorio_form_common_context(
    consultorio: Consultorio | None,
    *,
    errors: dict | None = None,
    form_data: dict | None = None,
    cabildo_defaults: dict | None = None,
    google_config: dict | None = None,
    google_calendars: list[dict] | None = None,
    google_calendar_error: str | None = None,
    google_service_account_email: str | None = None,
) -> dict:
    return {
        "consultorio": consultorio,
        "tipos": list(TipoConsultorio),
        "errors": errors or {},
        "form_data": form_data or {},
        "cabildo_defaults": cabildo_defaults or {
            "user": "",
            "password": "",
            "staff_id": "",
            "days": 21,
        },
        "google_config": google_config or (get_google_calendar_config(consultorio) if consultorio else default_google_calendar_config()),
        "google_weekdays": [(key, WEEKDAY_LABELS[key]) for key in WEEKDAY_KEYS],
        "google_calendars": google_calendars or [],
        "google_calendar_error": google_calendar_error,
        "google_service_account_email": google_service_account_email,
    }


def _load_google_calendars_for_tenant(tenant: Tenant, candidate_calendar_id: str | None = None) -> tuple[list[dict], str | None]:
    try:
        calendars = CalendarService().list_google_calendars(tenant, candidate_calendar_id)
        if not calendars:
            email = CalendarService().get_google_service_account_email(tenant)
            suffix = f" ({email})" if email else ""
            if candidate_calendar_id:
                return [], (
                    "No se pudo validar ese Calendar ID con Google. Verifica que el ID sea correcto y que el "
                    f"calendario este compartido con la service account{suffix} con permisos para modificar eventos."
                )
            return [], (
                "Google no devolvio calendarios en la lista de la service account. Para calendarios secundarios, "
                "pega el Calendar ID en el campo del consultorio y volve a presionar Actualizar calendarios. "
                f"Service account{suffix}."
            )
        return calendars, None
    except HTTPException as exc:
        return [], str(exc.detail)
    except Exception as exc:
        email = CalendarService().get_google_service_account_email(tenant)
        suffix = f" Service account: {email}." if email else ""
        return [], (
            "No se pudieron listar calendarios. Verifica que Google Calendar API este habilitada y que el "
            f"calendario este compartido con permisos suficientes.{suffix} Detalle: {type(exc).__name__}."
        )


async def consultorios_new_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await session.get(Tenant, user.tenant_id)
    google_calendars, google_calendar_error = _load_google_calendars_for_tenant(tenant) if tenant else ([], None)
    google_service_account_email = CalendarService().get_google_service_account_email(tenant) if tenant else None
    return _template(
        request,
        "tenant/consultorio_form.html",
        _consultorio_form_common_context(
            None,
            google_calendars=google_calendars,
            google_calendar_error=google_calendar_error,
            google_service_account_email=google_service_account_email,
        ),
    )


async def consultorios_new_post(
    request: Request,
    nombre: str = Form(...),
    tipo: str = Form(...),
    proveedor_turnos: str | None = Form(None),
    cab_user: str | None = Form(None),
    cab_password: str | None = Form(None),
    cab_staff_id: str | None = Form(None),
    cab_days: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    validate_csrf(request, csrf_token)
    provider = normalize_provider(proveedor_turnos)
    config_externa: dict | None = None
    form = await request.form()
    google_config = default_google_calendar_config()
    google_calendars: list[dict] = []
    google_calendar_error = None
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant:
        google_calendars, google_calendar_error = _load_google_calendars_for_tenant(tenant)
    google_service_account_email = CalendarService().get_google_service_account_email(tenant) if tenant else None
    if provider == "consultorio_movil":
        errors: dict[str, str] = {}
        if not cab_user:
            errors["cab_user"] = "El usuario es obligatorio."
        if not cab_password:
            errors["cab_password"] = "La contraseña es obligatoria."
        if not cab_staff_id:
            errors["cab_staff_id"] = "El staff id es obligatorio."
        days_value = 21
        if cab_days:
            try:
                days_value = int(cab_days)
            except ValueError:
                errors["cab_days"] = "Ingresa un numero valido de dias."
        if errors:
            return _template(
                request,
                "tenant/consultorio_form.html",
                _consultorio_form_common_context(
                    None,
                    errors=errors,
                    form_data={"nombre": nombre, "tipo": tipo, "proveedor_turnos": provider or ""},
                    cabildo_defaults={"user": cab_user or "", "password": cab_password or "", "staff_id": cab_staff_id or "", "days": cab_days or 21},
                    google_config=google_config,
                    google_calendars=google_calendars,
                    google_calendar_error=google_calendar_error,
                    google_service_account_email=google_service_account_email,
                ),
            )
        days_value = 21
        if cab_days:
            try:
                days_value = int(cab_days)
            except ValueError:
                days_value = 21
        config_externa = {
            "cabildo": {
                "user": cab_user.strip(),
                "password": cab_password.strip(),
                "staff_id": cab_staff_id.strip(),
                "days": days_value,
            }
        }
    elif provider == "google":
        errors = {}
        try:
            google_config = build_google_calendar_config_from_form(form)
        except ValueError as exc:
            errors["google_calendar"] = str(exc)
        if errors:
            return _template(
                request,
                "tenant/consultorio_form.html",
                _consultorio_form_common_context(
                    None,
                    errors=errors,
                    form_data={"nombre": nombre, "tipo": tipo, "proveedor_turnos": provider or ""},
                    google_config=google_config,
                    google_calendars=google_calendars,
                    google_calendar_error=google_calendar_error,
                    google_service_account_email=google_service_account_email,
                ),
            )
        config_externa = {"google_calendar": google_config}
    consultorio = Consultorio(
        tenant_id=user.tenant_id,
        nombre=nombre,
        tipo=TipoConsultorio(tipo),
        proveedor_turnos=provider,
        configuracion_externa=config_externa,
    )
    async with session.begin_nested():
        session.add(consultorio)
        await session.flush()
        metadata = None
        if provider == "consultorio_movil":
            metadata = {
                "cabildo": consultorio.configuracion_externa.get("cabildo") if consultorio.configuracion_externa else {},
            }
        elif provider == "google":
            metadata = {"google_calendar": {"calendar_id": google_config.get("calendar_id"), "schedule_updated": True}}
        await audit_log(
            session,
            request,
            user,
            action="create",
            entity="consultorio",
            entity_id=consultorio.id,
            metadata=metadata,
        )
    add_flash(request, "success", "Consultorio creado")
    return RedirectResponse("/t/consultorios", status_code=303)


async def consultorios_edit_get(
    request: Request,
    consultorio_id: int,
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    consultorio = await get_tenant_entity_or_404(
        session, Consultorio, consultorio_id, user.tenant_id
    )
    cabildo_cfg = (consultorio.configuracion_externa or {}).get("cabildo") or {}
    tenant = await session.get(Tenant, user.tenant_id)
    google_calendars, google_calendar_error = _load_google_calendars_for_tenant(tenant) if tenant else ([], None)
    google_service_account_email = CalendarService().get_google_service_account_email(tenant) if tenant else None
    return _template(
        request,
        "tenant/consultorio_form.html",
        _consultorio_form_common_context(
            consultorio,
            cabildo_defaults={
                "user": cabildo_cfg.get("user") or "",
                "password": cabildo_cfg.get("password") or "",
                "staff_id": cabildo_cfg.get("staff_id") or "",
                "days": cabildo_cfg.get("days") or 21,
            },
            google_calendars=google_calendars,
            google_calendar_error=google_calendar_error,
            google_service_account_email=google_service_account_email,
        ),
    )


async def consultorios_edit_post(
    request: Request,
    consultorio_id: int,
    nombre: str = Form(...),
    tipo: str = Form(...),
    proveedor_turnos: str | None = Form(None),
    cab_user: str | None = Form(None),
    cab_password: str | None = Form(None),
    cab_staff_id: str | None = Form(None),
    cab_days: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    validate_csrf(request, csrf_token)
    provider = normalize_provider(proveedor_turnos)
    form = await request.form()
    consultorio = await get_tenant_entity_or_404(
        session, Consultorio, consultorio_id, user.tenant_id
    )
    errors: dict[str, str] = {}
    tenant = await session.get(Tenant, user.tenant_id)
    google_calendars, google_calendar_error = _load_google_calendars_for_tenant(tenant) if tenant else ([], None)
    google_service_account_email = CalendarService().get_google_service_account_email(tenant) if tenant else None
    google_config = get_google_calendar_config(consultorio)
    existing_days = (
        (consultorio.configuracion_externa or {}).get("cabildo") or {}
    ).get("days") or 21
    days_value = existing_days
    if provider == "consultorio_movil":
        if not cab_user:
            errors["cab_user"] = "El usuario es obligatorio."
        if not cab_password:
            errors["cab_password"] = "La contraseña es obligatoria."
        if not cab_staff_id:
            errors["cab_staff_id"] = "El staff id es obligatorio."
        if cab_days:
            try:
                days_value = int(cab_days)
            except ValueError:
                errors["cab_days"] = "Ingresa un numero valido de dias."
        if errors:
            return _template(
                request,
                "tenant/consultorio_form.html",
                _consultorio_form_common_context(
                    consultorio,
                    errors=errors,
                    form_data={"nombre": nombre, "tipo": tipo, "proveedor_turnos": provider or ""},
                    cabildo_defaults={"user": cab_user or "", "password": cab_password or "", "staff_id": cab_staff_id or "", "days": cab_days or 21},
                    google_config=google_config,
                    google_calendars=google_calendars,
                    google_calendar_error=google_calendar_error,
                    google_service_account_email=google_service_account_email,
                ),
            )
    elif provider == "google":
        try:
            google_config = build_google_calendar_config_from_form(form)
        except ValueError as exc:
            errors["google_calendar"] = str(exc)
        if errors:
            return _template(
                request,
                "tenant/consultorio_form.html",
                _consultorio_form_common_context(
                    consultorio,
                    errors=errors,
                    form_data={"nombre": nombre, "tipo": tipo, "proveedor_turnos": provider or ""},
                    google_config=google_config,
                    google_calendars=google_calendars,
                    google_calendar_error=google_calendar_error,
                    google_service_account_email=google_service_account_email,
                ),
            )
    previous_cabildo = None
    if consultorio.configuracion_externa:
        previous_cabildo = (consultorio.configuracion_externa.get("cabildo") or {}).copy()
    async with session.begin_nested():
        consultorio.nombre = nombre
        consultorio.tipo = TipoConsultorio(tipo)
        consultorio.proveedor_turnos = provider
        config_externa = consultorio.configuracion_externa or {}
        if provider == "consultorio_movil":
            config_externa["cabildo"] = {
                "user": cab_user.strip(),
                "password": cab_password.strip(),
                "staff_id": cab_staff_id.strip(),
                "days": days_value,
            }
            consultorio.configuracion_externa = config_externa
            flag_modified(consultorio, "configuracion_externa")
        elif provider == "google":
            config_externa["google_calendar"] = google_config
            consultorio.configuracion_externa = config_externa
            flag_modified(consultorio, "configuracion_externa")
        metadata = None
        if provider == "consultorio_movil":
            metadata = {
                "cabildo_before": previous_cabildo,
                "cabildo_after": config_externa.get("cabildo") if config_externa else {},
            }
        elif provider == "google":
            metadata = {"google_calendar": {"calendar_id": google_config.get("calendar_id"), "schedule_updated": True}}
        await audit_log(
            session,
            request,
            user,
            action="update",
            entity="consultorio",
            entity_id=consultorio.id,
            metadata=metadata,
        )
    add_flash(request, "success", "Consultorio actualizado")
    return RedirectResponse(f"/t/consultorios/{consultorio_id}/edit", status_code=303)


async def consultorio_provider_test(
    request: Request,
    consultorio_id: int,
    proveedor_turnos: str | None = Form(None),
    cab_user: str | None = Form(None),
    cab_password: str | None = Form(None),
    cab_staff_id: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    validate_csrf(request, csrf_token)
    consultorio = await get_tenant_entity_or_404(
        session, Consultorio, consultorio_id, user.tenant_id
    )
    if proveedor_turnos != "consultorio_movil":
        return JSONResponse(
            {
                "ok": False,
                "message": "La prueba esta disponible para Consultorio Movil.",
                "slots": [],
            },
            status_code=400,
        )

    errors: list[str] = []
    if not _strip_optional(cab_user):
        errors.append("usuario")
    if not _strip_optional(cab_password):
        errors.append("password")
    if not _strip_optional(cab_staff_id):
        errors.append("id del profesional")
    if errors:
        return JSONResponse(
            {
                "ok": False,
                "message": "Completa los campos requeridos para probar: "
                + ", ".join(errors)
                + ".",
                "slots": [],
            },
            status_code=400,
        )

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404)

    test_consultorio = SimpleNamespace(
        tipo=consultorio.tipo,
        configuracion_externa={
            "cabildo": {
                "user": cab_user.strip(),
                "password": cab_password.strip(),
                "staff_id": cab_staff_id.strip(),
                "days": 3,
            }
        },
    )
    try:
        selections = await asyncio.to_thread(
            list_next_presential_slots,
            tenant,
            test_consultorio,
            30,
        )
    except CabildoConfigError as exc:
        return JSONResponse(
            {"ok": False, "message": str(exc), "slots": []},
            status_code=400,
        )
    except Exception:
        return JSONResponse(
            {
                "ok": False,
                "message": "No se pudo conectar con Consultorio Movil. Verifica credenciales y staff id.",
                "slots": [],
            },
            status_code=502,
        )

    slots = [
        {
            "option": index,
            "label": selection.label,
            "start_at": selection.start_at.isoformat(),
            "end_at": selection.end_at.isoformat(),
            "duration_minutes": selection.duration_minutes,
            "timezone": selection.timezone,
        }
        for index, selection in enumerate(selections, start=1)
    ]
    message = (
        "Conexion OK. No hay turnos disponibles en los proximos 3 dias."
        if not slots
        else f"Conexion OK. Se encontraron {len(slots)} turnos disponibles en los proximos 3 dias."
    )
    return JSONResponse(
        {
            "ok": True,
            "message": message,
            "lookahead_days": 3,
            "slots": slots,
        }
    )


async def consultorio_google_calendars(
    request: Request,
    consultorio_id: int,
    gcal_calendar_id: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    validate_csrf(request, csrf_token)
    await get_tenant_entity_or_404(session, Consultorio, consultorio_id, user.tenant_id)
    return await _google_calendars_response(session, user, gcal_calendar_id)


async def tenant_google_calendars(
    request: Request,
    gcal_calendar_id: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    validate_csrf(request, csrf_token)
    return await _google_calendars_response(session, user, gcal_calendar_id)


async def _google_calendars_response(
    session: AsyncSession,
    user: CurrentUser,
    gcal_calendar_id: str,
) -> JSONResponse:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404)
    calendars, error = _load_google_calendars_for_tenant(tenant, gcal_calendar_id.strip())
    service_account_email = CalendarService().get_google_service_account_email(tenant)
    if error:
        return JSONResponse(
            {
                "ok": False,
                "message": error,
                "calendars": [],
                "service_account_email": service_account_email,
            },
            status_code=400,
        )
    return JSONResponse(
        {
            "ok": True,
            "message": f"Se encontraron {len(calendars)} calendarios.",
            "calendars": calendars,
            "service_account_email": service_account_email,
        }
    )


async def consultorio_calendar_slots_get(
    request: Request,
    consultorio_id: int,
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    consultorio = await get_tenant_entity_or_404(session, Consultorio, consultorio_id, user.tenant_id)
    if CalendarService().resolve_provider_name(consultorio) != "google":
        raise HTTPException(status_code=400, detail="El consultorio no usa Google Calendar")
    today = now_ba().date()
    google_config = get_google_calendar_config(consultorio)
    return _template(
        request,
        "tenant/consultorio_calendar_slots.html",
        {
            "consultorio": consultorio,
            "google_config": google_config,
            "date_from": today.isoformat(),
            "date_to": (today + timedelta(days=14)).isoformat(),
            "exclude_holidays": True,
            "preview_slots": [],
            "result": None,
            "warnings": [],
            "errors": {},
        },
    )


async def consultorio_calendar_slots_post(
    request: Request,
    consultorio_id: int,
    date_from: str = Form(...),
    date_to: str = Form(...),
    action: str = Form("preview"),
    exclude_holidays: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    validate_csrf(request, csrf_token)
    consultorio = await get_tenant_entity_or_404(session, Consultorio, consultorio_id, user.tenant_id)
    if CalendarService().resolve_provider_name(consultorio) != "google":
        raise HTTPException(status_code=400, detail="El consultorio no usa Google Calendar")
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404)
    errors: dict[str, str] = {}
    result = None
    preview_slots = []
    warnings: list[str] = []
    logger.info(
        "calendar_slots_post_start tenant_id=%s consultorio_id=%s user_id=%s action=%s date_from=%s date_to=%s exclude_holidays=%s provider=%s",
        user.tenant_id,
        consultorio_id,
        user.id,
        action,
        date_from,
        date_to,
        bool(exclude_holidays),
        consultorio.proveedor_turnos,
    )
    try:
        start_day = date.fromisoformat(date_from)
        end_day = date.fromisoformat(date_to)
        holiday_service = HolidayService()
        if exclude_holidays:
            missing = holiday_service.missing_years(start_day, end_day)
            if missing:
                warnings.append("No hay feriados cargados para: " + ", ".join(str(year) for year in missing))
        google_config = get_google_calendar_config(consultorio)
        preview_slots = calculate_slots(
            google_config,
            start_day,
            end_day,
            exclude_argentina_holidays=bool(exclude_holidays),
            holiday_service=holiday_service,
        )
        logger.info(
            "calendar_slots_calculated tenant_id=%s consultorio_id=%s action=%s calendar_id=%s timezone=%s slots=%s warnings=%s",
            user.tenant_id,
            consultorio_id,
            action,
            _mask_calendar_id(google_config.get("calendar_id")),
            google_config.get("timezone"),
            len(preview_slots),
            len(warnings),
        )
    except ValueError as exc:
        errors["date_range"] = str(exc)
        logger.warning(
            "calendar_slots_validation_error tenant_id=%s consultorio_id=%s action=%s error=%s",
            user.tenant_id,
            consultorio_id,
            action,
            str(exc),
        )
    if not errors and action == "generate":
        try:
            logger.info(
                "calendar_slots_generate_call tenant_id=%s consultorio_id=%s slots=%s",
                tenant.id,
                consultorio.id,
                len(preview_slots),
            )
            result = await CalendarService().generate_available_slots(tenant, consultorio, preview_slots)
            logger.info(
                "calendar_slots_generate_result tenant_id=%s consultorio_id=%s result=%s",
                tenant.id,
                consultorio.id,
                _calendar_generation_log_result(result),
            )
            await audit_log(
                session,
                request,
                user,
                action="google_calendar_slots_generated",
                entity="consultorio",
                entity_id=consultorio.id,
                tenant_id=tenant.id,
                metadata={
                    "calendar_id": get_google_calendar_config(consultorio).get("calendar_id"),
                    "date_from": date_from,
                    "date_to": date_to,
                    "result": result,
                },
            )
        except Exception as exc:
            errors["generate"] = f"No se pudieron generar slots: {type(exc).__name__}"
            logger.exception(
                "calendar_slots_generate_unhandled_error tenant_id=%s consultorio_id=%s error=%s",
                tenant.id,
                consultorio.id,
                type(exc).__name__,
            )
    return _template(
        request,
        "tenant/consultorio_calendar_slots.html",
        {
            "consultorio": consultorio,
            "google_config": get_google_calendar_config(consultorio),
            "date_from": date_from,
            "date_to": date_to,
            "exclude_holidays": bool(exclude_holidays),
            "preview_slots": preview_slots,
            "result": result,
            "warnings": warnings,
            "errors": errors,
        },
    )


async def consultorios_delete(
    request: Request,
    consultorio_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("consultorio:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin_nested():
        consultorio = await get_tenant_entity_or_404(
            session, Consultorio, consultorio_id, user.tenant_id
        )
        consultorio.deleted_at = now_ba()
        consultorio.deleted_by = user.id
        await audit_log(
            session,
            request,
            user,
            action="delete",
            entity="consultorio",
            entity_id=consultorio.id,
        )
    add_flash(request, "success", "Consultorio eliminado")
    return RedirectResponse("/t/consultorios", status_code=303)


async def pacientes_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("patient:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    q = request.query_params.get("q", "").strip()
    obra = request.query_params.get("obra", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)
    limit = 10
    offset = (page - 1) * limit

    stmt = select(Paciente).where(
        Paciente.tenant_id == user.tenant_id, Paciente.deleted_at.is_(None)
    )
    if q:
        stmt = stmt.where(
            (Paciente.nombre.ilike(f"%{q}%"))
            | (Paciente.apellido.ilike(f"%{q}%"))
            | (Paciente.telefono.ilike(f"%{q}%"))
        )
    if obra:
        stmt = stmt.where(Paciente.obra_social.ilike(f"%{obra}%"))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt)
    total_pages = max(((total or 0) + limit - 1) // limit, 1)

    result = await session.execute(
        stmt.order_by(Paciente.created_at.desc()).limit(limit).offset(offset)
    )
    pacientes = list(result.scalars().all())
    query_string = urlencode({k: v for k, v in {"q": q, "obra": obra}.items() if v})
    return _template(
        request,
        "tenant/pacientes_list.html",
        {
            "pacientes": pacientes,
            "q": q,
            "obra": obra,
            "page": page,
            "total_pages": total_pages,
            "query_string": query_string,
        },
    )


async def pacientes_new_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("patient:write")),
) -> Response:
    return _template(request, "tenant/paciente_form.html", {"paciente": None})


async def pacientes_new_post(
    request: Request,
    nombre: str = Form(...),
    apellido: str = Form(...),
    telefono: str = Form(...),
    dni: str = Form(...),
    email: str = Form(...),
    obra_social: str | None = Form(None),
    insurance_number: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("patient:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    normalized_phone = _sanitize_phone(telefono)
    duplicate_result = await session.execute(
        select(Paciente).where(
            Paciente.tenant_id == user.tenant_id,
            Paciente.deleted_at.is_(None),
        )
    )
    duplicates = list(duplicate_result.scalars().all())
    existing = next(
        (
            p
            for p in duplicates
            if _sanitize_phone(p.telefono) == normalized_phone or (p.dni and p.dni == dni)
        ),
        None,
    )
    if existing is not None:
        add_flash(
            request,
            "warning",
            "Ya existe un paciente con ese telefono o DNI. Editalo en lugar de crearlo de nuevo.",
        )
        return RedirectResponse(f"/t/pacientes/{existing.id}/edit", status_code=303)

    paciente = Paciente(
        tenant_id=user.tenant_id,
        nombre=nombre,
        apellido=apellido,
        telefono=normalized_phone,
        dni=dni,
        email=email,
        obra_social=obra_social,
        insurance_number=insurance_number,
    )
    async with session.begin_nested():
        session.add(paciente)
        await session.flush()
        await audit_log(
            session,
            request,
            user,
            action="create",
            entity="paciente",
            entity_id=paciente.id,
        )
    add_flash(request, "success", "Paciente creado")
    return RedirectResponse("/t/pacientes", status_code=303)


async def pacientes_edit_get(
    request: Request,
    paciente_id: int,
    user: CurrentUser = Depends(require_permission("patient:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    paciente = await get_tenant_entity_or_404(
        session, Paciente, paciente_id, user.tenant_id
    )
    return _template(request, "tenant/paciente_form.html", {"paciente": paciente})


async def pacientes_edit_post(
    request: Request,
    paciente_id: int,
    nombre: str = Form(...),
    apellido: str = Form(...),
    telefono: str = Form(...),
    dni: str = Form(...),
    email: str = Form(...),
    obra_social: str | None = Form(None),
    insurance_number: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("patient:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    normalized_phone = _sanitize_phone(telefono)
    duplicate_result = await session.execute(
        select(Paciente).where(
            Paciente.tenant_id == user.tenant_id,
            Paciente.deleted_at.is_(None),
            Paciente.id != paciente_id,
        )
    )
    duplicates = list(duplicate_result.scalars().all())
    existing = next(
        (
            p
            for p in duplicates
            if _sanitize_phone(p.telefono) == normalized_phone or (p.dni and p.dni == dni)
        ),
        None,
    )
    if existing is not None:
        add_flash(
            request,
            "warning",
            "Ya existe otro paciente con ese telefono o DNI.",
        )
        return RedirectResponse(f"/t/pacientes/{paciente_id}/edit", status_code=303)

    async with session.begin_nested():
        paciente = await get_tenant_entity_or_404(
            session, Paciente, paciente_id, user.tenant_id
        )
        paciente.nombre = nombre
        paciente.apellido = apellido
        paciente.telefono = normalized_phone
        paciente.dni = dni
        paciente.email = email
        paciente.obra_social = obra_social
        paciente.insurance_number = insurance_number
        await audit_log(
            session,
            request,
            user,
            action="update",
            entity="paciente",
            entity_id=paciente.id,
        )
    add_flash(request, "success", "Paciente actualizado")
    return RedirectResponse("/t/pacientes", status_code=303)


async def pacientes_delete(
    request: Request,
    paciente_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("patient:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin_nested():
        paciente = await get_tenant_entity_or_404(
            session, Paciente, paciente_id, user.tenant_id
        )
        paciente.deleted_at = now_ba()
        paciente.deleted_by = user.id
        await audit_log(
            session,
            request,
            user,
            action="delete",
            entity="paciente",
            entity_id=paciente.id,
        )
    add_flash(request, "success", "Paciente eliminado")
    return RedirectResponse("/t/pacientes", status_code=303)


async def turnos_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("appointment:read")),
) -> RedirectResponse:
    del user
    target = "/t/appointments"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=303)


async def turnos_detail(
    request: Request,
    turno_id: int,
    user: CurrentUser = Depends(require_permission("appointment:read")),
) -> RedirectResponse:
    del request, user
    return RedirectResponse(f"/t/appointments/{turno_id}", status_code=303)


async def _conversation_listing_context(
    request: Request,
    session: AsyncSession,
    *,
    tenant_id: int | None,
    scope_prefix: str,
    show_tenant: bool,
) -> dict:
    legacy_queue = (request.query_params.get("queue") or "").strip().lower()
    selected_status = (request.query_params.get("status") or "").strip().lower()
    selected_category = _normalize_operational_category(request.query_params.get("category"))
    if request.query_params.get("category") in (None, ""):
        selected_category = ""
    # Compatibilidad con filtros legacy del panel anterior.
    if legacy_queue:
        queue_to_status = {
            "all_pending": "pending",
            "finished": "resolved",
        }
        queue_to_category = {
            "turno_presencial": "turno_presencial",
            "turno_virtual": "turno_virtual",
            "receta_orden": "receta_orden",
            "otra_consulta": "otra_consulta",
            "derivacion_humana": "derivacion_humana",
        }
        if not selected_status:
            selected_status = queue_to_status.get(legacy_queue, "pending")
        if not selected_category and legacy_queue in queue_to_category:
            selected_category = queue_to_category[legacy_queue]
    if selected_status not in {"pending", "resolved", "all"}:
        selected_status = "pending"
    selected_subtype = (request.query_params.get("subtype") or "").strip().upper()
    media_only = (request.query_params.get("media_only") or "").strip() == "1"
    human_only = (request.query_params.get("human_only") or "").strip() == "1"
    time_window = (request.query_params.get("time_window") or "").strip().lower()
    if time_window not in {"", "today", "last24h"}:
        time_window = ""
    start_date = (request.query_params.get("start_date") or "").strip()
    end_date = (request.query_params.get("end_date") or "").strip()
    tenant_filter = (request.query_params.get("tenant_id") or "").strip()
    selected_tenant_id = int(tenant_filter) if show_tenant and tenant_filter.isdigit() else None

    state_stmt = select(EstadoConversacion).order_by(EstadoConversacion.updated_at.desc())
    history_stmt = select(ConversationHistory).order_by(ConversationHistory.resolved_at.desc(), ConversationHistory.id.desc())
    paciente_stmt = select(Paciente).where(Paciente.deleted_at.is_(None))
    tenant_stmt = select(Tenant).where(Tenant.deleted_at.is_(None))
    if tenant_id is not None:
        state_stmt = state_stmt.where(EstadoConversacion.tenant_id == tenant_id)
        history_stmt = history_stmt.where(ConversationHistory.tenant_id == tenant_id)
        paciente_stmt = paciente_stmt.where(Paciente.tenant_id == tenant_id)
        tenant_stmt = tenant_stmt.where(Tenant.id == tenant_id)
    elif selected_tenant_id is not None:
        state_stmt = state_stmt.where(EstadoConversacion.tenant_id == selected_tenant_id)
        history_stmt = history_stmt.where(ConversationHistory.tenant_id == selected_tenant_id)
        paciente_stmt = paciente_stmt.where(Paciente.tenant_id == selected_tenant_id)
        tenant_stmt = tenant_stmt.where(Tenant.id == selected_tenant_id)

    states = list((await session.execute(state_stmt)).scalars().all())
    history_states = list((await session.execute(history_stmt)).scalars().all())
    pacientes = list((await session.execute(paciente_stmt)).scalars().all())
    tenants = list((await session.execute(tenant_stmt)).scalars().all())

    pacientes_by_phone = {(p.tenant_id, _sanitize_phone(p.telefono)): p for p in pacientes}
    pacientes_by_id = {p.id: p for p in pacientes}
    tenants_by_id = {t.id: t for t in tenants}

    unresolved_states = [state for state in states if (state.status or "active").lower() != "finished"]

    def _matches_common(item, *, resolved: bool) -> bool:
        operational_category = _resolve_operational_category(
            getattr(item, "operational_category", None),
            getattr(item, "conversation_category", None),
            getattr(item, "pending_reason", None),
            bool(getattr(item, "requires_human_review", False)),
        )
        if selected_category and operational_category != selected_category:
            return False
        if selected_subtype and (getattr(item, "conversation_subtype", "") or "") != selected_subtype:
            return False
        if media_only and not bool(getattr(item, "has_media", False)):
            return False
        if human_only and not bool(getattr(item, "requires_human_review", False)):
            return False
        time_value = getattr(item, "resolved_at", None) if resolved else getattr(item, "updated_at", None)
        if not _within_operational_window(time_value, time_window, start_date, end_date):
            return False
        return True

    filtered_current = [state for state in unresolved_states if _matches_common(state, resolved=False)]
    filtered_history = [item for item in history_states if _matches_common(item, resolved=True)]

    rows = []
    if selected_status in {"pending", "all"}:
        for state in filtered_current:
            paciente = pacientes_by_phone.get((state.tenant_id, _sanitize_phone(state.telefono)))
            operational_category = _resolve_operational_category(
                state.operational_category,
                state.conversation_category,
                state.pending_reason,
                bool(state.requires_human_review),
            )
            status_label, status_tone = _status_meta(state.status)
            tenant = tenants_by_id.get(state.tenant_id)
            rows.append(
                {
                    "source": "state",
                    "telefono": state.telefono,
                    "tenant_id": state.tenant_id,
                    "tenant_nombre": tenant.nombre if tenant else "-",
                    "updated_at": state.updated_at,
                    "summary_text": state.pending_message or state.last_patient_message or "-",
                    "pending_reason": state.pending_reason or "sin_clasificar",
                    "pending_reason_label": OPERATIONAL_CATEGORY_LABELS.get(_normalize_operational_category(state.pending_reason), state.pending_reason or "-"),
                    "has_media": bool(state.has_media),
                    "requires_human_review": bool(state.requires_human_review),
                    "paciente_nombre": f"{paciente.nombre} {paciente.apellido}".strip() if paciente else "-",
                    "operational_category": operational_category,
                    "operational_label": OPERATIONAL_CATEGORY_LABELS.get(operational_category, "Sin clasificar"),
                    "operational_tone": _operational_tone(operational_category),
                    "category_label": CATEGORY_LABELS.get(state.conversation_category or "", "-"),
                    "subtype_label": SUBTYPE_LABELS.get(state.conversation_subtype or "", state.conversation_subtype or "-"),
                    "whatsapp_link": _build_whatsapp_link(state.telefono),
                    "detail_href": f"{scope_prefix}/conversation-states/{state.telefono}",
                    "resolve_href": f"{scope_prefix}/conversation-states/{state.telefono}/resolve",
                    "status_label": status_label,
                    "status_tone": status_tone,
                    "is_recent": _is_recent_interaction(state.updated_at),
                    "is_stale": _is_stale_pending(state.updated_at, state.status),
                    "ai_summary": get_ai_summary_from_context(state.contexto_json, mask_sensitive=True),
                }
            )
    if selected_status in {"resolved", "all"}:
        for item in filtered_history:
            paciente = pacientes_by_id.get(item.patient_id) if item.patient_id else pacientes_by_phone.get((item.tenant_id, _sanitize_phone(item.telefono)))
            operational_category = _resolve_operational_category(
                item.operational_category,
                item.conversation_category,
                item.pending_reason,
                bool(item.requires_human_review),
            )
            tenant = tenants_by_id.get(item.tenant_id)
            rows.append(
                {
                    "source": "history",
                    "telefono": item.telefono,
                    "tenant_id": item.tenant_id,
                    "tenant_nombre": tenant.nombre if tenant else "-",
                    "updated_at": item.resolved_at,
                    "summary_text": item.pending_message or item.last_patient_message or "-",
                    "pending_reason": item.pending_reason or "sin_clasificar",
                    "pending_reason_label": OPERATIONAL_CATEGORY_LABELS.get(_normalize_operational_category(item.pending_reason), item.pending_reason or "-"),
                    "has_media": bool(item.has_media),
                    "requires_human_review": bool(item.requires_human_review),
                    "paciente_nombre": f"{paciente.nombre} {paciente.apellido}".strip() if paciente else "-",
                    "operational_category": operational_category,
                    "operational_label": OPERATIONAL_CATEGORY_LABELS.get(operational_category, "Sin clasificar"),
                    "operational_tone": _operational_tone(operational_category),
                    "category_label": CATEGORY_LABELS.get(item.conversation_category or "", "-"),
                    "subtype_label": SUBTYPE_LABELS.get(item.conversation_subtype or "", item.conversation_subtype or "-"),
                    "whatsapp_link": _build_whatsapp_link(item.telefono),
                    "detail_href": f"{scope_prefix}/conversation-states/history/{item.id}",
                    "resolve_href": None,
                    "status_label": "Resuelta",
                    "status_tone": "success",
                    "is_recent": _is_recent_interaction(item.resolved_at),
                    "is_stale": False,
                    "ai_summary": get_ai_summary_from_context(item.contexto_json, mask_sensitive=True),
                }
            )

    rows.sort(key=lambda item: item["updated_at"] or now_ba(), reverse=True)

    unresolved_counts = {
        key: len(
            [
                state
                for state in unresolved_states
                if _resolve_operational_category(
                    state.operational_category,
                    state.conversation_category,
                    state.pending_reason,
                    bool(state.requires_human_review),
                )
                == key
            ]
        )
        for key in OPERATIONAL_CATEGORY_LABELS.keys()
    }
    subtype_values = sorted(
        {
            (subtype or "").strip()
            for subtype in (
                [state.conversation_subtype for state in states]
                + [history.conversation_subtype for history in history_states]
            )
            if (subtype or "").strip()
        }
    )

    base_filter_pairs = []
    if selected_category:
        base_filter_pairs.append(("category", selected_category))
    if selected_subtype:
        base_filter_pairs.append(("subtype", selected_subtype))
    if media_only:
        base_filter_pairs.append(("media_only", "1"))
    if human_only:
        base_filter_pairs.append(("human_only", "1"))
    if time_window:
        base_filter_pairs.append(("time_window", time_window))
    if start_date:
        base_filter_pairs.append(("start_date", start_date))
    if end_date:
        base_filter_pairs.append(("end_date", end_date))
    if selected_tenant_id is not None:
        base_filter_pairs.append(("tenant_id", str(selected_tenant_id)))

    def _status_url(value: str) -> str:
        params = [("status", value), *base_filter_pairs]
        return f"{scope_prefix}/conversation-states?{urlencode(params)}"

    status_urls = {value: _status_url(value) for value in {"pending", "resolved", "all"}}
    kpis = {
        "pending": len(unresolved_states),
        "resolved": len(history_states),
        "human": unresolved_counts["derivacion_humana"],
        "presential": unresolved_counts["turno_presencial"],
        "virtual": unresolved_counts["turno_virtual"],
        "prescription": unresolved_counts["receta_orden"],
    }
    return {
        "rows": rows,
        "selected_status": selected_status,
        "selected_category": selected_category,
        "selected_subtype": selected_subtype,
        "media_only": media_only,
        "human_only": human_only,
        "time_window": time_window,
        "start_date": start_date,
        "end_date": end_date,
        "selected_tenant_id": selected_tenant_id,
        "subtype_values": subtype_values,
        "subtype_labels": SUBTYPE_LABELS,
        "operational_category_labels": OPERATIONAL_CATEGORY_LABELS,
        "counts": kpis,
        "status_urls": status_urls,
        "category_counts": unresolved_counts,
        "show_tenant": show_tenant,
        "tenants": tenants,
        "scope_prefix": scope_prefix,
    }


async def conversation_states(
    request: Request,
    user: CurrentUser = Depends(require_permission("conversation:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    context = await _conversation_listing_context(
        request,
        session,
        tenant_id=user.tenant_id,
        scope_prefix="/t",
        show_tenant=False,
    )
    return _template(request, "tenant/conversation_states.html", context)


def _sanitize_phone(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _build_whatsapp_link(phone: str | None) -> str | None:
    digits = _sanitize_phone(phone)
    if not digits:
        return None
    return f"https://wa.me/{digits}"


def _resolve_timezone(name: str) -> tzinfo | None:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _format_local_time(value: datetime | None) -> str:
    if not value:
        return ""
    tz = _resolve_timezone("America/Argentina/Buenos_Aires") or timezone(timedelta(hours=-3))
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(tz).strftime("%Y-%m-%d %H:%M")


OPERATIONAL_CATEGORY_LABELS = {
    "turno_presencial": "Turno presencial",
    "turno_virtual": "Turno virtual",
    "receta_orden": "Receta / orden",
    "otra_consulta": "Otra consulta",
    "derivacion_humana": "Derivacion humana",
    "sin_clasificar": "Sin clasificar",
}

OPERATIONAL_FROM_CATEGORY = {
    ConversationCategory.PRESENTIAL_APPOINTMENT: "turno_presencial",
    ConversationCategory.VIRTUAL_APPOINTMENT: "turno_virtual",
    ConversationCategory.PRESCRIPTION_OR_ORDER: "receta_orden",
    ConversationCategory.OTHER_QUERY: "otra_consulta",
    ConversationCategory.HUMAN_HANDOFF: "derivacion_humana",
}


def _normalize_operational_category(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    legacy_map = {
        "presential_appointment": "turno_presencial",
        "virtual_appointment": "turno_virtual",
        "prescription_or_order": "receta_orden",
        "other_query": "otra_consulta",
        "human_handoff": "derivacion_humana",
    }
    if normalized in legacy_map:
        return legacy_map[normalized]
    return normalized if normalized in OPERATIONAL_CATEGORY_LABELS else "sin_clasificar"


def _resolve_operational_category(
    operational_category: str | None,
    conversation_category: str | None,
    pending_reason: str | None,
    requires_human_review: bool = False,
) -> str:
    if operational_category and operational_category.strip().lower() in OPERATIONAL_CATEGORY_LABELS:
        return operational_category.strip().lower()
    pending = (pending_reason or "").strip().lower()
    if pending in OPERATIONAL_CATEGORY_LABELS:
        return pending
    if pending == "humano":
        return "derivacion_humana"
    if conversation_category:
        mapped = OPERATIONAL_FROM_CATEGORY.get(conversation_category)
        if mapped:
            return mapped
    if requires_human_review:
        return "derivacion_humana"
    return "sin_clasificar"


def _operational_tone(value: str) -> str:
    return {
        "turno_presencial": "success",
        "turno_virtual": "info",
        "receta_orden": "warning",
        "otra_consulta": "neutral",
        "derivacion_humana": "warning",
        "sin_clasificar": "neutral",
    }.get(value, "neutral")


def _status_meta(status_value: str | None) -> tuple[str, str]:
    status = (status_value or "active").strip().lower()
    if status == "pending":
        return "Pendiente", "warning"
    if status == "finished":
        return "Resuelta", "success"
    return "Activa", "info"


def _is_recent_interaction(value: datetime | None) -> bool:
    if value is None:
        return False
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current >= now_ba() - timedelta(hours=24)


def _is_stale_pending(value: datetime | None, status: str | None) -> bool:
    if (status or "").lower() != "pending" or value is None:
        return False
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current < now_ba() - timedelta(hours=24)


def _within_operational_window(
    value: datetime | None,
    time_window: str,
    start_date: str,
    end_date: str,
) -> bool:
    if value is None:
        return False
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = now_ba()
    if time_window == "today":
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start <= current < end
    if time_window == "last24h":
        return current >= local_now - timedelta(hours=24)
    if start_date:
        try:
            start = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0, microsecond=0)
            if current < start:
                return False
        except ValueError:
            pass
    if end_date:
        try:
            end = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, microsecond=999999)
            if current > end:
                return False
        except ValueError:
            pass
    return True


async def conversation_state_detail(
    request: Request,
    telefono: str,
    user: CurrentUser = Depends(require_permission("conversation:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    result = await session.execute(
        select(EstadoConversacion).where(
            EstadoConversacion.tenant_id == user.tenant_id,
            EstadoConversacion.telefono == telefono,
        )
    )
    state = result.scalar_one_or_none()
    if state is None:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")
    phone_digits = _sanitize_phone(telefono)
    paciente_result = await session.execute(
        select(Paciente).where(
            Paciente.tenant_id == user.tenant_id,
            Paciente.deleted_at.is_(None),
        )
    )
    pacientes = list(paciente_result.scalars().all())
    paciente = next(
        (item for item in pacientes if _sanitize_phone(item.telefono) == phone_digits),
        None,
    )
    contexto_pretty = json.dumps(sanitize_context_for_display(state.contexto_json), ensure_ascii=True, indent=2)
    status = (state.status or "active").lower()
    return _template(
        request,
        "tenant/conversation_state_detail.html",
        {
            "record": state,
            "is_history": False,
            "paciente": paciente,
            "tenant": await get_entity_or_404(session, Tenant, user.tenant_id),
            "contexto_pretty": contexto_pretty,
            "status": status,
            "category_label": CATEGORY_LABELS.get(state.conversation_category or "", "-"),
            "subtype_label": SUBTYPE_LABELS.get(
                state.conversation_subtype or "", state.conversation_subtype or "-"
            ),
            "operational_category": _resolve_operational_category(
                state.operational_category,
                state.conversation_category,
                state.pending_reason,
                bool(state.requires_human_review),
            ),
            "operational_labels": OPERATIONAL_CATEGORY_LABELS,
            "whatsapp_link": _build_whatsapp_link(state.telefono),
            "scope_prefix": "/t",
            "ai_summary": get_ai_summary_from_context(state.contexto_json, mask_sensitive=False),
        },
    )


async def conversation_history_detail(
    request: Request,
    history_id: int,
    user: CurrentUser = Depends(require_permission("conversation:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    history = await session.get(ConversationHistory, history_id)
    if history is None or history.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")
    paciente = None
    if history.patient_id:
        paciente = await session.get(Paciente, history.patient_id)
    if paciente is None:
        paciente_result = await session.execute(
            select(Paciente).where(
                Paciente.tenant_id == user.tenant_id,
                Paciente.deleted_at.is_(None),
            )
        )
        pacientes = list(paciente_result.scalars().all())
        paciente = next((item for item in pacientes if _sanitize_phone(item.telefono) == _sanitize_phone(history.telefono)), None)
    return _template(
        request,
        "tenant/conversation_state_detail.html",
        {
            "record": history,
            "is_history": True,
            "paciente": paciente,
            "tenant": await get_entity_or_404(session, Tenant, user.tenant_id),
            "contexto_pretty": json.dumps(sanitize_context_for_display(history.contexto_json), ensure_ascii=True, indent=2),
            "status": "finished",
            "category_label": CATEGORY_LABELS.get(history.conversation_category or "", "-"),
            "subtype_label": SUBTYPE_LABELS.get(
                history.conversation_subtype or "", history.conversation_subtype or "-"
            ),
            "operational_category": _resolve_operational_category(
                history.operational_category,
                history.conversation_category,
                history.pending_reason,
                bool(history.requires_human_review),
            ),
            "operational_labels": OPERATIONAL_CATEGORY_LABELS,
            "whatsapp_link": _build_whatsapp_link(history.telefono),
            "scope_prefix": "/t",
            "ai_summary": get_ai_summary_from_context(history.contexto_json, mask_sensitive=False),
        },
    )


async def conversation_state_resolve(
    request: Request,
    telefono: str,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("conversation:read")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    repo = ConversacionRepository(session)
    async with session.begin_nested():
        state = await repo.mark_resolved(
            user.tenant_id,
            telefono,
            resolved_by=user.id,
            close_reason="manual_resolve",
        )
        await audit_log(
            session,
            request,
            user,
            action="conversation_resolved",
            entity="conversation_state",
            entity_id=None,
            metadata={
                "telefono": telefono,
                "tenant_id": user.tenant_id,
                "pending_reason": getattr(state, "pending_reason", None),
            },
        )
    add_flash(request, "success", "Conversacion marcada como finalizada")
    return RedirectResponse("/t/conversation-states?status=resolved", status_code=303)


async def conversation_state_review_update(
    request: Request,
    telefono: str,
    operational_category: str = Form(""),
    manual_note: str = Form(""),
    ai_corrected_intent: str = Form(""),
    ai_review_note: str = Form(""),
    status_action: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("conversation:read")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    repo = ConversacionRepository(session)
    operational_category = _normalize_operational_category(operational_category)
    async with session.begin_nested():
        state = await repo.update_operational_review(
            user.tenant_id,
            telefono,
            operational_category=operational_category,
            manual_note=(manual_note or "").strip() or None,
        )
        if state is None:
            raise HTTPException(status_code=404, detail="Conversacion no encontrada")
        _apply_ai_review(
            state,
            corrected_intent=ai_corrected_intent,
            review_note=ai_review_note,
            reviewed_by=user.id,
        )
        if status_action == "pending":
            state = await repo.mark_pending_manual(user.tenant_id, telefono)
            audit_action = "conversation_marked_pending"
        elif status_action == "resolved":
            state = await repo.mark_resolved(
                user.tenant_id,
                telefono,
                resolved_by=user.id,
                close_reason="manual_review_resolve",
            )
            audit_action = "conversation_resolved"
        else:
            audit_action = "conversation_review_updated"
        await audit_log(
            session,
            request,
            user,
            action=audit_action,
            entity="conversation_state",
            metadata={
                "telefono": telefono,
                "tenant_id": user.tenant_id,
                "operational_category": operational_category,
                "manual_note": (manual_note or "").strip() or None,
                "ai_corrected_intent": (ai_corrected_intent or "").strip() or None,
                "ai_review_note": (ai_review_note or "").strip() or None,
                "status_action": status_action or None,
            },
        )
    add_flash(request, "success", "Revision de conversacion actualizada")
    return RedirectResponse(f"/t/conversation-states/{telefono}", status_code=303)


def _apply_ai_review(
    state: EstadoConversacion,
    *,
    corrected_intent: str,
    review_note: str,
    reviewed_by: int | None,
) -> None:
    corrected_intent = (corrected_intent or "").strip()
    review_note = (review_note or "").strip()
    if not corrected_intent and not review_note:
        return
    context = dict(state.contexto_json or {})
    context["ai_review"] = {
        "human_corrected_intent": corrected_intent or None,
        "review_note": review_note or None,
        "reviewed_by": reviewed_by,
        "reviewed_at": now_ba().isoformat(),
    }
    state.contexto_json = context
    flag_modified(state, "contexto_json")


async def settings_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
    whatsapp_settings = _parse_whatsapp_settings(tenant)
    webhook_url = _build_tenant_whatsapp_webhook_url(
        request, tenant.id, whatsapp_settings.get("twilio_webhook_secret")
    )
    return _template(
        request,
        "tenant/settings.html",
        {
            "tenant": tenant,
            "errors": {},
            "form_data": {},
            "whatsapp_settings": whatsapp_settings,
            "twilio_webhook_url": webhook_url,
        },
    )


async def settings_post(
    request: Request,
    nombre: str = Form(...),
    fantasy_name: str | None = Form(None),
    first_name: str | None = Form(None),
    last_name: str | None = Form(None),
    address: str | None = Form(None),
    postal_code: str | None = Form(None),
    phone: str | None = Form(None),
    whatsapp_number: str | None = Form(None),
    twilio_account_sid: str | None = Form(None),
    twilio_auth_token: str | None = Form(None),
    twilio_whatsapp_number: str | None = Form(None),
    twilio_webhook_secret: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    errors: dict[str, str] = {}
    cleaned = {
        "nombre": nombre.strip(),
        "fantasy_name": _strip_optional(fantasy_name),
        "first_name": _strip_optional(first_name),
        "last_name": _strip_optional(last_name),
        "address": _strip_optional(address),
        "postal_code": _validate_digits(postal_code, "postal_code", errors),
        "phone": _validate_digits(phone, "phone", errors),
        "whatsapp_number": _strip_optional(whatsapp_number),
    }
    whatsapp_cleaned = {
        "twilio_account_sid": _strip_optional(twilio_account_sid),
        "twilio_auth_token": _strip_optional(twilio_auth_token),
        "twilio_whatsapp_number": _strip_optional(twilio_whatsapp_number),
        "twilio_webhook_secret": _strip_optional(twilio_webhook_secret),
    }
    has_any_twilio = any(whatsapp_cleaned.values())
    has_all_twilio = all(whatsapp_cleaned.values())
    if has_any_twilio and not has_all_twilio:
        errors["twilio"] = "Completa SID, Auth Token y numero de WhatsApp para usar Twilio propio."
    if not cleaned["nombre"]:
        errors["nombre"] = "El nombre es obligatorio."
    if errors:
        tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
        display_whatsapp = _parse_whatsapp_settings(tenant)
        for key, value in whatsapp_cleaned.items():
            if value is not None:
                display_whatsapp[key] = value
        if not display_whatsapp.get("twilio_webhook_secret"):
            display_whatsapp["twilio_webhook_secret"] = _ensure_webhook_secret(None)
        webhook_url = _build_tenant_whatsapp_webhook_url(
            request, tenant.id, display_whatsapp.get("twilio_webhook_secret")
        )
        return _template(
            request,
            "tenant/settings.html",
            {
                "tenant": tenant,
                "errors": errors,
                "form_data": cleaned,
                "whatsapp_settings": display_whatsapp,
                "twilio_webhook_url": webhook_url,
            },
        )
    async with session.begin_nested():
        tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
        if cleaned["whatsapp_number"]:
            exists_stmt = select(Tenant.id).where(
                Tenant.whatsapp_number == cleaned["whatsapp_number"],
                Tenant.id != tenant.id,
                Tenant.deleted_at.is_(None),
            )
            exists = await session.execute(exists_stmt)
            if exists.scalar_one_or_none() is not None:
                errors["whatsapp_number"] = "Ese WhatsApp ya esta registrado."
        if errors:
            display_whatsapp = _parse_whatsapp_settings(tenant)
            for key, value in whatsapp_cleaned.items():
                if value is not None:
                    display_whatsapp[key] = value
            if not display_whatsapp.get("twilio_webhook_secret"):
                display_whatsapp["twilio_webhook_secret"] = _ensure_webhook_secret(None)
            webhook_url = _build_tenant_whatsapp_webhook_url(
                request, tenant.id, display_whatsapp.get("twilio_webhook_secret")
            )
            return _template(
                request,
                "tenant/settings.html",
                {
                    "tenant": tenant,
                    "errors": errors,
                    "form_data": cleaned,
                    "whatsapp_settings": display_whatsapp,
                    "twilio_webhook_url": webhook_url,
                },
            )
        if not cleaned["whatsapp_number"]:
            cleaned["whatsapp_number"] = tenant.whatsapp_number
        changes = _collect_tenant_profile_changes(tenant, cleaned)
        previous_whatsapp_settings = tenant.whatsapp_settings or {}
        twilio_changed = (
            (previous_whatsapp_settings.get("twilio_account_sid") or "") != (whatsapp_cleaned["twilio_account_sid"] or "")
            or (previous_whatsapp_settings.get("twilio_auth_token") or "") != (whatsapp_cleaned["twilio_auth_token"] or "")
            or (previous_whatsapp_settings.get("twilio_whatsapp_number") or "") != (whatsapp_cleaned["twilio_whatsapp_number"] or "")
            or (previous_whatsapp_settings.get("twilio_webhook_secret") or "") != (whatsapp_cleaned["twilio_webhook_secret"] or "")
        )
        webhook_secret = _ensure_webhook_secret(
            whatsapp_cleaned["twilio_webhook_secret"] or previous_whatsapp_settings.get("twilio_webhook_secret")
        )
        tenant.nombre = cleaned["nombre"]
        tenant.fantasy_name = cleaned["fantasy_name"]
        tenant.first_name = cleaned["first_name"]
        tenant.last_name = cleaned["last_name"]
        tenant.address = cleaned["address"]
        tenant.postal_code = cleaned["postal_code"]
        tenant.phone = cleaned["phone"]
        tenant.whatsapp_number = cleaned["whatsapp_number"]
        tenant.whatsapp_settings = {
            "twilio_account_sid": whatsapp_cleaned["twilio_account_sid"] or "",
            "twilio_auth_token": whatsapp_cleaned["twilio_auth_token"] or "",
            "twilio_whatsapp_number": whatsapp_cleaned["twilio_whatsapp_number"] or "",
            "twilio_webhook_secret": webhook_secret,
        }
        if twilio_changed:
            changes["twilio_settings"] = {"from": "configured", "to": "updated"}
        await audit_log(
            session,
            request,
            user,
            action="update_profile",
            entity="tenant",
            entity_id=tenant.id,
            metadata=changes,
        )
    add_flash(request, "success", "Configuracion actualizada")
    return RedirectResponse("/t/settings", status_code=303)


def _parse_payment_settings(tenant: Tenant) -> dict:
    settings = tenant.payment_settings or {}
    return {
        "enabled": bool(settings.get("enabled", False)),
        "require_before_appointment": bool(settings.get("require_before_appointment", False)),
        "amount": settings.get("amount", ""),
        "currency": settings.get("currency", "ARS"),
        "mp_access_token": settings.get("mp_access_token", ""),
        "mp_webhook_secret": settings.get("mp_webhook_secret", ""),
        "public_text": settings.get("public_text", ""),
    }


def _parse_calendar_settings(tenant: Tenant) -> dict:
    settings = tenant.calendar_settings or {}
    return {
        "google_calendar_id": settings.get("google_calendar_id", ""),
        "calendar_tags": ",".join(settings.get("calendar_tags", []) or []),
        "default_timezone": settings.get("default_timezone", "America/Argentina/Buenos_Aires"),
        "virtual_meet_enabled": bool(settings.get("virtual_meet_enabled", False)),
        "google_credentials_json": settings.get("google_credentials_json", ""),
        "google_delegated_user": settings.get("google_delegated_user", ""),
    }


def _parse_whatsapp_settings(tenant: Tenant) -> dict:
    settings = tenant.whatsapp_settings or {}
    return {
        "twilio_account_sid": settings.get("twilio_account_sid", ""),
        "twilio_auth_token": settings.get("twilio_auth_token", ""),
        "twilio_whatsapp_number": settings.get("twilio_whatsapp_number", ""),
        "twilio_webhook_secret": settings.get("twilio_webhook_secret", ""),
    }


async def payment_settings_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
    return _template(
        request,
        "tenant/settings_payments.html",
        {"tenant": tenant, "payment_settings": _parse_payment_settings(tenant)},
    )


async def payment_settings_post(
    request: Request,
    enabled: str | None = Form(None),
    require_before_appointment: str | None = Form(None),
    amount: str = Form(""),
    currency: str = Form("ARS"),
    mp_access_token: str = Form(""),
    mp_webhook_secret: str = Form(""),
    public_text: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin_nested():
        tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
        tenant.payment_settings = {
            "enabled": bool(enabled),
            "require_before_appointment": bool(require_before_appointment),
            "amount": amount.strip(),
            "currency": currency.strip() or "ARS",
            "mp_access_token": mp_access_token.strip(),
            "mp_webhook_secret": mp_webhook_secret.strip(),
            "public_text": public_text.strip(),
        }
        await audit_log(
            session,
            request,
            user,
            action="update",
            entity="payment_settings",
            entity_id=tenant.id,
        )
    add_flash(request, "success", "Configuracion de pagos actualizada")
    return RedirectResponse("/t/settings/payments", status_code=303)


async def calendar_settings_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
    return _template(
        request,
        "tenant/settings_calendar.html",
        {"tenant": tenant, "calendar_settings": _parse_calendar_settings(tenant)},
    )


async def calendar_settings_post(
    request: Request,
    calendar_tags: str = Form(""),
    default_timezone: str = Form("America/Argentina/Buenos_Aires"),
    virtual_meet_enabled: str | None = Form(None),
    google_credentials_json: str = Form(""),
    google_delegated_user: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    tags = [tag.strip() for tag in calendar_tags.split(",") if tag.strip()]
    async with session.begin_nested():
        tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
        previous_settings = tenant.calendar_settings or {}
        tenant.calendar_settings = {
            # Compatibilidad con tenants antiguos: el calendario operativo nuevo
            # se configura por consultorio, pero no borramos el fallback existente.
            "google_calendar_id": previous_settings.get("google_calendar_id", ""),
            "calendar_tags": tags,
            "default_timezone": default_timezone.strip() or "UTC",
            "virtual_meet_enabled": bool(virtual_meet_enabled),
            "google_credentials_json": google_credentials_json.strip(),
            "google_delegated_user": google_delegated_user.strip(),
        }
        await audit_log(
            session,
            request,
            user,
            action="update",
            entity="calendar_settings",
            entity_id=tenant.id,
        )
    add_flash(request, "success", "Configuracion de calendario actualizada")
    return RedirectResponse("/t/settings/calendar", status_code=303)


async def calendar_settings_test(
    request: Request,
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
):
    tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
    settings = tenant.calendar_settings or {}
    default_timezone = settings.get("default_timezone") or "America/Argentina/Buenos_Aires"
    tz = _resolve_timezone(default_timezone) or timezone(timedelta(hours=-3))

    result = await session.execute(
        select(Consultorio)
        .where(
            Consultorio.tenant_id == tenant.id,
            Consultorio.deleted_at.is_(None),
        )
        .order_by(Consultorio.tipo.asc())
    )
    consultorios = list(result.scalars().all())
    consultorio = next(
        (item for item in consultorios if item.tipo == TipoConsultorio.VIRTUAL),
        consultorios[0] if consultorios else None,
    )
    if consultorio is None:
        return JSONResponse(
            {"error": "Necesitas un consultorio para probar el calendario."},
            status_code=400,
        )

    start = now_ba().astimezone(tz)
    end = start + timedelta(days=14)
    service = CalendarService()
    slots = await service.list_available_slots(tenant, consultorio, start, end)
    payload = [
        {
            "slot_id": slot.slot_id,
            "start_at": slot.start_at.isoformat(),
            "end_at": slot.end_at.isoformat(),
            "timezone": slot.timezone,
            "provider": slot.provider,
            "calendar_id": slot.calendar_id,
        }
        for slot in slots
    ]
    return JSONResponse({"count": len(payload), "items": payload})


async def notifications_settings(
    request: Request,
    user: CurrentUser = Depends(require_permission("settings:write")),
) -> Response:
    return _template(
        request,
        "tenant/settings_notifications.html",
        {},
    )


async def audit_logs(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    action = request.query_params.get("action", "").strip()
    entity = request.query_params.get("entity", "").strip()

    stmt = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity:
        stmt = stmt.where(AuditLog.entity == entity)

    result = await session.execute(stmt.order_by(desc(AuditLog.created_at)).limit(100))
    logs = list(result.scalars().all())
    log_times = {log.id: _format_local_time(log.created_at) for log in logs}
    return _template(
        request,
        "tenant/audit_logs.html",
        {"logs": logs, "action": action, "entity": entity, "log_times": log_times},
    )


async def notifications_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("notification:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    stmt = select(Notification).where(Notification.tenant_id == user.tenant_id)
    result = await session.execute(stmt.order_by(desc(Notification.created_at)))
    items = list(result.scalars().all())
    return _template(
        request,
        "tenant/notifications.html",
        {"items": items},
    )


async def notifications_mark_read(
    request: Request,
    notification_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("notification:read")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    stmt = select(Notification).where(
        Notification.id == notification_id,
        Notification.tenant_id == user.tenant_id,
    )
    result = await session.execute(stmt)
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notificacion no encontrada")
    await mark_notification_read(session, notification)
    await session.commit()
    return RedirectResponse("/t/notifications", status_code=303)


async def payments_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("payment:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    status_filter = request.query_params.get("status", "").strip()
    q = request.query_params.get("q", "").strip()

    stmt = (
        select(Payment, Paciente, Turno)
        .join(Paciente, Payment.patient_id == Paciente.id)
        .outerjoin(Turno, Payment.appointment_id == Turno.id)
        .where(Payment.tenant_id == user.tenant_id)
    )
    if status_filter:
        try:
            stmt = stmt.where(Payment.status == PaymentStatus(status_filter))
        except ValueError:
            status_filter = ""
    if q:
        stmt = stmt.where(
            (Paciente.nombre.ilike(f"%{q}%"))
            | (Paciente.apellido.ilike(f"%{q}%"))
            | (Paciente.telefono.ilike(f"%{q}%"))
        )
    result = await session.execute(stmt.order_by(Payment.created_at.desc()))
    rows = list(result.all())
    return _template(
        request,
        "tenant/payments_list.html",
        {"rows": rows, "status_filter": status_filter, "q": q},
    )


async def payment_detail(
    request: Request,
    payment_id: int,
    user: CurrentUser = Depends(require_permission("payment:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    stmt = (
        select(Payment, Paciente, Turno)
        .join(Paciente, Payment.patient_id == Paciente.id)
        .outerjoin(Turno, Payment.appointment_id == Turno.id)
        .where(Payment.id == payment_id, Payment.tenant_id == user.tenant_id)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Pago no encontrado")
    events_result = await session.execute(
        select(PaymentEvent)
        .where(PaymentEvent.payment_id == payment_id)
        .order_by(desc(PaymentEvent.created_at))
    )
    events = list(events_result.scalars().all())
    return _template(
        request,
        "tenant/payment_detail.html",
        {"row": row, "events": events},
    )


async def appointments_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("appointment:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    date_str = request.query_params.get("date", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    consultorio_id = request.query_params.get("consultorio_id", "").strip()
    date_str, start, end = _selected_day(date_str)

    stmt = (
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(
            Turno.tenant_id == user.tenant_id,
            Consultorio.tenant_id == user.tenant_id,
            Turno.deleted_at.is_(None),
        )
    )
    if status_filter:
        try:
            stmt = stmt.where(Turno.status == AppointmentStatus(status_filter))
        except ValueError:
            status_filter = ""
    if consultorio_id:
        try:
            stmt = stmt.where(Consultorio.id == int(consultorio_id))
        except ValueError:
            consultorio_id = ""
    if date_str:
        stmt = stmt.where(Turno.fecha_hora >= start, Turno.fecha_hora < end)

    result = await session.execute(stmt.order_by(Turno.fecha_hora.desc()))
    rows = list(result.all())
    consultorios = list(
        (
            await session.execute(
                select(Consultorio).where(
                    Consultorio.tenant_id == user.tenant_id,
                    Consultorio.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    daily_summary = {
        "count": len(rows),
        "virtuales": sum(1 for turno, _, _ in rows if _turno_type_label(turno) == "Virtual"),
        "presenciales": sum(1 for turno, _, _ in rows if _turno_type_label(turno) == "Presencial"),
    }
    consultorio_summary = []
    for consultorio in consultorios:
        assigned = [
            turno
            for turno, _, row_consultorio in rows
            if row_consultorio.id == consultorio.id
            and turno.status not in {AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED}
        ]
        consultorio_summary.append({"consultorio": consultorio, "count": len(assigned)})
    return _template(
        request,
        "tenant/appointments_list.html",
        {
            "rows": rows,
            "consultorios": consultorios,
            "status_filter": status_filter,
            "consultorio_id": consultorio_id,
            "date": date_str,
            "daily_summary": daily_summary,
            "consultorio_summary": consultorio_summary,
        },
    )


async def appointment_detail(
    request: Request,
    turno_id: int,
    user: CurrentUser = Depends(require_permission("appointment:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    stmt = (
        select(Turno, Paciente, Consultorio, Tenant)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .join(Tenant, Turno.tenant_id == Tenant.id)
        .where(
            Turno.id == turno_id,
            Turno.tenant_id == user.tenant_id,
            Consultorio.tenant_id == user.tenant_id,
        )
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    return _template(
        request,
        "tenant/appointment_detail.html",
        {
            "row": row,
            "status_meta": _turno_status_label(row[0]),
            "provider_meta": _turno_provider_label(row[0]),
            "type_label": _turno_type_label(row[0]),
            "scope_prefix": "/t",
            "cancel_href": f"/t/appointments/{row[0].id}/cancel",
            "resend_href": f"/t/appointments/{row[0].id}/resend",
        },
    )


async def appointment_cancel(
    request: Request,
    turno_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("appointment:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    stmt = (
        select(Turno, Consultorio, Tenant)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .join(Tenant, Consultorio.tenant_id == Tenant.id)
        .where(
            Turno.id == turno_id,
            Turno.tenant_id == user.tenant_id,
            Consultorio.tenant_id == user.tenant_id,
        )
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    turno, consultorio, tenant = row
    try:
        await AppointmentService(session).cancel_turno(request, tenant, consultorio, turno)
    except Exception:
        add_flash(request, "error", "No se pudo cancelar el turno externo. No se modifico el turno local.")
        return RedirectResponse(f"/t/appointments/{turno_id}", status_code=303)
    add_flash(request, "success", "Turno cancelado y liberado")
    return RedirectResponse(f"/t/appointments/{turno_id}", status_code=303)


async def appointment_resend(
    request: Request,
    turno_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("appointment:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    stmt = (
        select(Turno, Paciente, Consultorio, Tenant)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .join(Tenant, Consultorio.tenant_id == Tenant.id)
        .where(
            Turno.id == turno_id,
            Turno.tenant_id == user.tenant_id,
            Consultorio.tenant_id == user.tenant_id,
        )
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    turno = row[0]
    paciente = row[1]
    consultorio = row[2]
    tenant = row[3]
    start_at = turno.start_at or turno.fecha_hora
    fecha_texto = start_at.strftime('%Y-%m-%d %H:%M') if start_at else "-"
    message = f"Confirmacion de turno: {consultorio.nombre} el {fecha_texto}."
    MessagingService().send_whatsapp(paciente.telefono, message, tenant=tenant)
    add_flash(request, "success", "Confirmacion reenviada")
    return RedirectResponse(f"/t/appointments/{turno_id}", status_code=303)

