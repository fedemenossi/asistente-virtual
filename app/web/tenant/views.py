from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.audit import audit_log
from app.core.csrf import validate_csrf
from app.core.db import get_async_session
from app.core.notifications import mark_notification_read
from app.core.security import CurrentUser, require_permission, require_tenant_admin
from app.core.templates import base_context, templates
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
from app.services.appointment_service import AppointmentService
from app.services.messaging_service import MessagingService


def _template(request: Request, name: str, context: dict) -> Response:
    base = base_context(request)
    base.update(context)
    return templates.TemplateResponse(request, name, base)


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
    conversaciones = await session.scalar(
        select(func.count()).select_from(EstadoConversacion).where(
            EstadoConversacion.tenant_id == user.tenant_id
        )
    )
    return _template(
        request,
        "tenant/dashboard.html",
        {
            "kpis": {
                "pacientes": pacientes_total or 0,
                "consultorios": consultorios_total or 0,
                "turnos": turnos_total or 0,
                "conversaciones": conversaciones or 0,
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
        consultorio.deleted_at = datetime.now(timezone.utc)
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
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("patient:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    paciente = Paciente(
        tenant_id=user.tenant_id,
        nombre=nombre,
        apellido=apellido,
        telefono=telefono,
        dni=dni,
        email=email,
        obra_social=obra_social,
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
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("patient:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin_nested():
        paciente = await get_tenant_entity_or_404(
            session, Paciente, paciente_id, user.tenant_id
        )
        paciente.nombre = nombre
        paciente.apellido = apellido
        paciente.telefono = telefono
        paciente.dni = dni
        paciente.email = email
        paciente.obra_social = obra_social
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
        paciente.deleted_at = datetime.now(timezone.utc)
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
        select(Turno, Paciente, Consultorio)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
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
    result = await session.execute(
        select(EstadoConversacion)
        .where(EstadoConversacion.tenant_id == user.tenant_id)
        .order_by(EstadoConversacion.updated_at.desc())
    )
    states = list(result.scalars().all())
    return _template(
        request,
        "tenant/conversation_states.html",
        {"states": states},
    )


async def settings_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
    return _template(request, "tenant/settings.html", {"tenant": tenant})


async def settings_post(
    request: Request,
    nombre: str = Form(...),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("settings:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin_nested():
        tenant = await get_entity_or_404(session, Tenant, user.tenant_id)
        tenant.nombre = nombre
        await audit_log(
            session,
            request,
            user,
            action="update",
            entity="tenant_settings",
            entity_id=tenant.id,
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
        "default_timezone": settings.get("default_timezone", "UTC"),
        "virtual_meet_enabled": bool(settings.get("virtual_meet_enabled", False)),
        "google_credentials_json": settings.get("google_credentials_json", ""),
        "google_delegated_user": settings.get("google_delegated_user", ""),
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
    default_timezone: str = Form("UTC"),
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
    return _template(
        request,
        "tenant/audit_logs.html",
        {"logs": logs, "action": action, "entity": entity},
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
    turno = await get_tenant_entity_or_404(session, Turno, turno_id, user.tenant_id)
    consultorio = await get_entity_or_404(session, Consultorio, turno.consultorio_id)
    tenant = await get_entity_or_404(session, Tenant, consultorio.tenant_id)
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
    start_at = turno.start_at or turno.fecha_hora
    fecha_texto = start_at.strftime('%Y-%m-%d %H:%M') if start_at else "-"
    message = f"Confirmacion de turno: {consultorio.nombre} el {fecha_texto}."
    MessagingService().send_whatsapp(paciente.telefono, message)
    add_flash(request, "success", "Confirmacion reenviada")
    return RedirectResponse(f"/t/appointments/{turno_id}", status_code=303)

