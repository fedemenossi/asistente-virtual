from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import secrets
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
from app.models.conversacion import EstadoConversacion
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, Turno
from app.models.notification import Notification
from app.models.payment import Payment, PaymentStatus
from app.models.payment_event import PaymentEvent
from app.repositories.conversacion_repository import ConversacionRepository
from app.services.appointment_service import AppointmentService
from app.services.conversation_intents import (
    CATEGORY_LABELS,
    ConversationCategory,
    SUBTYPE_LABELS,
)
from app.services.messaging_service import MessagingService
from app.services.calendar_service import CalendarService


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


async def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
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
            Consultorio.tenant_id == user.tenant_id,
            Consultorio.deleted_at.is_(None),
            Turno.deleted_at.is_(None),
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
    return _template(
        request,
        "tenant/dashboard.html",
        {
            "kpis": {
                "pacientes": pacientes_total or 0,
                "consultorios": consultorios_total or 0,
                "turnos": turnos_total or 0,
                "conversaciones": conversaciones_abiertas,
            }
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


async def consultorios_new_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("consultorio:write")),
) -> Response:
    return _template(
        request,
        "tenant/consultorio_form.html",
        {
            "consultorio": None,
            "tipos": list(TipoConsultorio),
            "errors": {},
            "form_data": {},
            "cabildo_defaults": {
                "user": "",
                "password": "",
                "staff_id": "",
                "days": 21,
            },
        },
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
    config_externa: dict | None = None
    if proveedor_turnos == "consultorio_movil":
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
                {
                    "consultorio": None,
                    "tipos": list(TipoConsultorio),
                    "errors": errors,
                    "form_data": {
                        "nombre": nombre,
                        "tipo": tipo,
                        "proveedor_turnos": proveedor_turnos or "",
                    },
                    "cabildo_defaults": {
                        "user": cab_user or "",
                        "password": cab_password or "",
                        "staff_id": cab_staff_id or "",
                        "days": cab_days or 21,
                    },
                },
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
    consultorio = Consultorio(
        tenant_id=user.tenant_id,
        nombre=nombre,
        tipo=TipoConsultorio(tipo),
        proveedor_turnos=proveedor_turnos,
        configuracion_externa=config_externa,
    )
    async with session.begin_nested():
        session.add(consultorio)
        await session.flush()
        metadata = None
        if proveedor_turnos == "consultorio_movil":
            metadata = {
                "cabildo": consultorio.configuracion_externa.get("cabildo") if consultorio.configuracion_externa else {},
            }
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
    return _template(
        request,
        "tenant/consultorio_form.html",
        {
            "consultorio": consultorio,
            "tipos": list(TipoConsultorio),
            "errors": {},
            "form_data": {},
            "cabildo_defaults": {
                "user": cabildo_cfg.get("user") or "",
                "password": cabildo_cfg.get("password") or "",
                "staff_id": cabildo_cfg.get("staff_id") or "",
                "days": cabildo_cfg.get("days") or 21,
            },
        },
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
    consultorio = await get_tenant_entity_or_404(
        session, Consultorio, consultorio_id, user.tenant_id
    )
    errors: dict[str, str] = {}
    existing_days = (
        (consultorio.configuracion_externa or {}).get("cabildo") or {}
    ).get("days") or 21
    days_value = existing_days
    if proveedor_turnos == "consultorio_movil":
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
                {
                    "consultorio": consultorio,
                    "tipos": list(TipoConsultorio),
                    "errors": errors,
                    "form_data": {
                        "nombre": nombre,
                        "tipo": tipo,
                        "proveedor_turnos": proveedor_turnos or "",
                    },
                    "cabildo_defaults": {
                        "user": cab_user or "",
                        "password": cab_password or "",
                        "staff_id": cab_staff_id or "",
                        "days": cab_days or 21,
                    },
                },
            )
    previous_cabildo = None
    if consultorio.configuracion_externa:
        previous_cabildo = (consultorio.configuracion_externa.get("cabildo") or {}).copy()
    async with session.begin_nested():
        consultorio.nombre = nombre
        consultorio.tipo = TipoConsultorio(tipo)
        consultorio.proveedor_turnos = proveedor_turnos
        config_externa = consultorio.configuracion_externa or {}
        if proveedor_turnos == "consultorio_movil":
            config_externa["cabildo"] = {
                "user": cab_user.strip(),
                "password": cab_password.strip(),
                "staff_id": cab_staff_id.strip(),
                "days": days_value,
            }
            consultorio.configuracion_externa = config_externa
            flag_modified(consultorio, "configuracion_externa")
        metadata = None
        if proveedor_turnos == "consultorio_movil":
            metadata = {
                "cabildo_before": previous_cabildo,
                "cabildo_after": config_externa.get("cabildo") if config_externa else {},
            }
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
    return RedirectResponse("/t/consultorios", status_code=303)


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
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tipo = request.query_params.get("tipo", "")
    estado = request.query_params.get("estado", "")
    date_str = request.query_params.get("date", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)
    limit = 10
    offset = (page - 1) * limit

    stmt = (
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(Consultorio.tenant_id == user.tenant_id)
    )
    stmt = stmt.where(
        Turno.deleted_at.is_(None),
        Consultorio.deleted_at.is_(None),
        Paciente.deleted_at.is_(None),
    )
    if tipo:
        stmt = stmt.where(Turno.tipo == tipo)
    if estado:
        stmt = stmt.where(Turno.estado == estado)
    if date_str:
        try:
            parsed = datetime.fromisoformat(date_str)
            start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            stmt = stmt.where(Turno.fecha_hora >= start, Turno.fecha_hora < end)
        except ValueError:
            date_str = ""

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt)
    total_pages = max(((total or 0) + limit - 1) // limit, 1)

    result = await session.execute(
        stmt.order_by(Turno.fecha_hora.desc()).limit(limit).offset(offset)
    )
    rows = result.all()
    query_string = urlencode(
        {k: v for k, v in {"tipo": tipo, "estado": estado, "date": date_str}.items() if v}
    )
    return _template(
        request,
        "tenant/turnos_list.html",
        {
            "rows": rows,
            "tipo": tipo,
            "estado": estado,
            "date": date_str,
            "page": page,
            "total_pages": total_pages,
            "query_string": query_string,
        },
    )


async def turnos_detail(
    request: Request,
    turno_id: int,
    user: CurrentUser = Depends(require_permission("appointment:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    stmt = (
        select(Turno, Paciente, Consultorio, Tenant)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .join(Tenant, Consultorio.tenant_id == Tenant.id)
        .where(Turno.id == turno_id, Consultorio.tenant_id == user.tenant_id)
    )
    stmt = stmt.where(
        Turno.deleted_at.is_(None),
        Consultorio.deleted_at.is_(None),
        Paciente.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    return _template(request, "tenant/turno_detail.html", {"row": row})


async def conversation_states(
    request: Request,
    user: CurrentUser = Depends(require_permission("conversation:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    selected_queue = (request.query_params.get("queue") or "all_pending").strip().lower()
    selected_category = (request.query_params.get("category") or "").strip().upper()
    selected_subtype = (request.query_params.get("subtype") or "").strip().upper()
    media_only = (request.query_params.get("media_only") or "").strip() == "1"
    human_only = (request.query_params.get("human_only") or "").strip() == "1"
    allowed_queues = {
        "turno_presencial",
        "turno_virtual",
        "receta_orden",
        "otra_consulta",
        "all_pending",
        "finished",
    }
    if selected_queue not in allowed_queues:
        selected_queue = "all_pending"
    allowed_categories = {
        "",
        ConversationCategory.PRESENTIAL_APPOINTMENT,
        ConversationCategory.VIRTUAL_APPOINTMENT,
        ConversationCategory.PRESCRIPTION_OR_ORDER,
        ConversationCategory.OTHER_QUERY,
        ConversationCategory.HUMAN_HANDOFF,
    }
    if selected_category not in allowed_categories:
        selected_category = ""

    result = await session.execute(
        select(EstadoConversacion)
        .where(EstadoConversacion.tenant_id == user.tenant_id)
        .order_by(EstadoConversacion.updated_at.desc())
    )
    states = list(result.scalars().all())
    pacientes_result = await session.execute(
        select(Paciente).where(
            Paciente.tenant_id == user.tenant_id,
            Paciente.deleted_at.is_(None),
        )
    )
    pacientes = list(pacientes_result.scalars().all())
    pacientes_by_phone = {_sanitize_phone(p.telefono): p for p in pacientes}

    pending_by_reason: dict[str, list[EstadoConversacion]] = {
        "turno_presencial": [],
        "turno_virtual": [],
        "receta_orden": [],
        "otra_consulta": [],
    }
    all_pending_states: list[EstadoConversacion] = []
    finished_states: list[EstadoConversacion] = []
    active_states: list[EstadoConversacion] = []
    active_cutoff = now_ba() - timedelta(minutes=30)
    for state in states:
        status = (state.status or "active").lower()
        if status == "pending":
            reason = (state.pending_reason or "otra_consulta").lower()
            if reason not in pending_by_reason:
                reason = "otra_consulta"
            pending_by_reason[reason].append(state)
            all_pending_states.append(state)
        elif status == "finished":
            finished_states.append(state)
        else:
            updated = state.updated_at
            if updated is None:
                continue
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc).astimezone(active_cutoff.tzinfo)
            if updated >= active_cutoff:
                active_states.append(state)

    if selected_queue == "finished":
        filtered_states = finished_states
    elif selected_queue == "all_pending":
        filtered_states = all_pending_states
    else:
        filtered_states = pending_by_reason.get(selected_queue, [])

    def _match_filters(state: EstadoConversacion) -> bool:
        if selected_category and (state.conversation_category or "") != selected_category:
            return False
        if selected_subtype and (state.conversation_subtype or "") != selected_subtype:
            return False
        if media_only and not bool(state.has_media):
            return False
        if human_only and not bool(state.requires_human_review):
            return False
        return True

    filtered_states = [state for state in filtered_states if _match_filters(state)]

    category_counts = {
        key: len([s for s in all_pending_states if (s.conversation_category or "") == key])
        for key in CATEGORY_LABELS.keys()
    }
    subtype_values = sorted(
        {
            (state.conversation_subtype or "").strip()
            for state in states
            if (state.conversation_subtype or "").strip()
        }
    )

    from urllib.parse import urlencode

    base_filter_pairs = []
    if selected_category:
        base_filter_pairs.append(("category", selected_category))
    if selected_subtype:
        base_filter_pairs.append(("subtype", selected_subtype))
    if media_only:
        base_filter_pairs.append(("media_only", "1"))
    if human_only:
        base_filter_pairs.append(("human_only", "1"))

    def _queue_url(queue: str) -> str:
        params = [("queue", queue), *base_filter_pairs]
        return f"/t/conversation-states?{urlencode(params)}"

    queue_urls = {queue: _queue_url(queue) for queue in allowed_queues}

    rows = []
    for state in filtered_states:
        paciente = pacientes_by_phone.get(_sanitize_phone(state.telefono))
        rows.append(
            {
                "state": state,
                "paciente": paciente,
                "paciente_nombre": (
                    f"{paciente.nombre} {paciente.apellido}".strip()
                    if paciente is not None
                    else "-"
                ),
                "category_label": CATEGORY_LABELS.get(state.conversation_category or "", "-"),
                "subtype_label": SUBTYPE_LABELS.get(
                    state.conversation_subtype or "", state.conversation_subtype or "-"
                ),
                "whatsapp_link": _build_whatsapp_link(state.telefono),
            }
        )

    return _template(
        request,
        "tenant/conversation_states.html",
        {
            "rows": rows,
            "selected_queue": selected_queue,
            "pending_states": all_pending_states,
            "finished_states": finished_states,
            "active_states": active_states,
            "queue_urls": queue_urls,
            "selected_category": selected_category,
            "selected_subtype": selected_subtype,
            "media_only": media_only,
            "human_only": human_only,
            "category_labels": CATEGORY_LABELS,
            "subtype_labels": SUBTYPE_LABELS,
            "category_counts": category_counts,
            "subtype_values": subtype_values,
            "counts": {
                "pending": len(all_pending_states),
                "finished": len(finished_states),
                "active": len(active_states),
                "turno_presencial": len(pending_by_reason["turno_presencial"]),
                "turno_virtual": len(pending_by_reason["turno_virtual"]),
                "receta_orden": len(pending_by_reason["receta_orden"]),
                "otra_consulta": len(pending_by_reason["otra_consulta"]),
            },
        },
    )


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
    contexto_pretty = json.dumps(state.contexto_json or {}, ensure_ascii=True, indent=2)
    status = (state.status or "active").lower()
    return _template(
        request,
        "tenant/conversation_state_detail.html",
        {
            "state": state,
            "paciente": paciente,
            "contexto_pretty": contexto_pretty,
            "status": status,
            "category_label": CATEGORY_LABELS.get(state.conversation_category or "", "-"),
            "subtype_label": SUBTYPE_LABELS.get(
                state.conversation_subtype or "", state.conversation_subtype or "-"
            ),
            "whatsapp_link": _build_whatsapp_link(state.telefono),
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
        state = await repo.mark_resolved(user.tenant_id, telefono, resolved_by=user.id)
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
    return RedirectResponse("/t/conversation-states?queue=finished", status_code=303)


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
    google_calendar_id: str = Form(""),
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
        tenant.calendar_settings = {
            "google_calendar_id": google_calendar_id.strip(),
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

    stmt = (
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(Consultorio.tenant_id == user.tenant_id, Turno.deleted_at.is_(None))
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
        try:
            parsed = datetime.fromisoformat(date_str)
            start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            stmt = stmt.where(Turno.fecha_hora >= start, Turno.fecha_hora < end)
        except ValueError:
            date_str = ""

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
    return _template(
        request,
        "tenant/appointments_list.html",
        {
            "rows": rows,
            "consultorios": consultorios,
            "status_filter": status_filter,
            "consultorio_id": consultorio_id,
            "date": date_str,
        },
    )


async def appointment_detail(
    request: Request,
    turno_id: int,
    user: CurrentUser = Depends(require_permission("appointment:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    stmt = (
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(Turno.id == turno_id, Consultorio.tenant_id == user.tenant_id)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    return _template(request, "tenant/appointment_detail.html", {"row": row})


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
        .where(Turno.id == turno_id, Consultorio.tenant_id == user.tenant_id)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    turno, consultorio, tenant = row
    await AppointmentService(session).cancel_turno(request, tenant, consultorio, turno)
    add_flash(request, "success", "Turno cancelado")
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
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .where(Turno.id == turno_id, Consultorio.tenant_id == user.tenant_id)
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

