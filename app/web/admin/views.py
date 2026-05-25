from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.csrf import validate_csrf
from app.core.db import get_async_session
from app.core.features import FEATURE_REGISTRY
from app.core.notifications import mark_notification_read
from app.core.security import CurrentUser, hash_password, require_permission, require_super_admin
from app.core.templates import base_context, templates
from app.core.timezone import now_ba
from app.core.ui import add_flash
from app.core.tenancy import get_entity_or_404, set_current_tenant_id
from app.models.audit_log import AuditLog
from app.models.consultorio import Consultorio
from app.models.conversacion import EstadoConversacion
from app.models.conversation_history import ConversationHistory
from app.models.paciente import Paciente
from app.models.payment import Payment, PaymentStatus
from app.models.payment_event import PaymentEvent
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, Turno
from app.models.user import User, UserRole
from app.models.notification import Notification
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.paciente_repository import PacienteRepository
from app.repositories.tenant_repository import TenantRepository
from app.services.conversation_service import ConversationService
from app.services.ai_intent_classifier import SUPPORTED_INTENTS
from app.services.tenant_ai_settings_service import (
    AI_SETTINGS_DEFAULTS,
    get_effective_ai_settings,
    mask_api_key,
    validate_ai_settings,
)
from app.services.tenant_feature_service import TenantFeatureService
from app.services.tenant_service import TenantService
from app.web.tenant import views as tenant_conversation_views


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


def _tenant_form_context(
    request: Request,
    tenant: Tenant | None,
    errors: dict[str, str],
    form_data: dict,
) -> dict:
    ai_settings = form_data.get("ai_settings")
    if not isinstance(ai_settings, dict):
        ai_settings = get_effective_ai_settings(tenant) if tenant is not None else dict(AI_SETTINGS_DEFAULTS)
    return {
        "tenant": tenant,
        "errors": errors,
        "form_data": form_data,
        "ai_settings": ai_settings,
        "ai_api_key_masked": mask_api_key(ai_settings.get("api_key")),
        "ai_allowed_intents": sorted(SUPPORTED_INTENTS),
    }


async def _parse_ai_settings_form(
    request: Request,
    *,
    existing_settings: dict | None = None,
) -> tuple[dict, str | None]:
    form = await request.form()
    selected_intents = [
        str(value)
        for value in form.getlist("ai_allowed_intents")
        if str(value).strip()
    ]
    data = {
        "enabled": form.get("ai_enabled") == "1",
        "provider": form.get("ai_provider") or "openai",
        "api_key": (form.get("ai_api_key") or "").strip(),
        "model": form.get("ai_model") or AI_SETTINGS_DEFAULTS["model"],
        "min_confidence": form.get("ai_min_confidence") or AI_SETTINGS_DEFAULTS["min_confidence"],
        "timeout_seconds": form.get("ai_timeout_seconds") or AI_SETTINGS_DEFAULTS["timeout_seconds"],
        "agent_name": form.get("ai_agent_name") or AI_SETTINGS_DEFAULTS["agent_name"],
        "system_prompt": form.get("ai_system_prompt") or "",
        "personality": form.get("ai_personality") or AI_SETTINGS_DEFAULTS["personality"],
        "allowed_intents": selected_intents or list(AI_SETTINGS_DEFAULTS["allowed_intents"]),
        "handoff_on_low_confidence": form.get("ai_handoff_on_low_confidence") == "1",
        "max_tokens": form.get("ai_max_tokens") or AI_SETTINGS_DEFAULTS["max_tokens"],
        "temperature": form.get("ai_temperature") or AI_SETTINGS_DEFAULTS["temperature"],
        "tools_enabled": form.get("ai_tools_enabled") == "1",
        "availability_lookup_enabled": form.get("ai_availability_lookup_enabled") == "1",
        "max_offered_slots": form.get("ai_max_offered_slots") or AI_SETTINGS_DEFAULTS["max_offered_slots"],
        "require_confirmation_before_booking": form.get("ai_require_confirmation_before_booking") == "1",
    }
    try:
        cleaned = validate_ai_settings(
            data,
            existing_settings=existing_settings,
            allow_global_fallback=True,
        )
    except ValueError as exc:
        fallback = dict(AI_SETTINGS_DEFAULTS)
        fallback.update(data)
        if existing_settings and not fallback.get("api_key"):
            fallback["api_key"] = existing_settings.get("api_key", "")
        return fallback, str(exc)
    return cleaned, None


def _resolve_timezone(name: str) -> timezone | None:
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


async def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    now = now_ba()
    seven_days = now + timedelta(days=7)
    tenants_active = await session.scalar(
        select(func.count())
        .select_from(Tenant)
        .where(Tenant.activo.is_(True), Tenant.deleted_at.is_(None))
    )
    pacientes_total = await session.scalar(
        select(func.count()).select_from(Paciente).where(Paciente.deleted_at.is_(None))
    )
    turnos_7d = await session.scalar(
        select(func.count())
        .select_from(Turno)
        .where(
            Turno.fecha_hora >= now,
            Turno.fecha_hora <= seven_days,
            Turno.deleted_at.is_(None),
        )
    )
    conversaciones = await session.scalar(
        select(func.count()).select_from(EstadoConversacion)
    )

    return _template(
        request,
        "admin/dashboard.html",
        {
            "kpis": {
                "tenants": tenants_active or 0,
                "pacientes": pacientes_total or 0,
                "turnos": turnos_7d or 0,
                "conversaciones": conversaciones or 0,
            }
        },
    )


async def tenants_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    q = request.query_params.get("q", "").strip()
    status_filter = request.query_params.get("status", "")
    show_deleted = request.query_params.get("show_deleted", "")
    page = max(int(request.query_params.get("page", "1") or 1), 1)
    limit = 10
    offset = (page - 1) * limit

    stmt = select(Tenant)
    if q:
        stmt = stmt.where(Tenant.nombre.ilike(f"%{q}%"))
    if status_filter == "active":
        stmt = stmt.where(Tenant.activo.is_(True))
    if status_filter == "inactive":
        stmt = stmt.where(Tenant.activo.is_(False))
    if not show_deleted:
        stmt = stmt.where(Tenant.deleted_at.is_(None))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt)
    total_pages = max(((total or 0) + limit - 1) // limit, 1)

    result = await session.execute(
        stmt.order_by(Tenant.created_at.desc()).limit(limit).offset(offset)
    )
    tenants = list(result.scalars().all())
    query_string = urlencode(
        {
            k: v
            for k, v in {
                "q": q,
                "status": status_filter,
                "show_deleted": show_deleted,
            }.items()
            if v
        }
    )
    return _template(
        request,
        "admin/tenants_list.html",
        {
            "tenants": tenants,
            "q": q,
            "status_filter": status_filter,
            "page": page,
            "total_pages": total_pages,
            "query_string": query_string,
            "show_deleted": show_deleted,
        },
    )


async def tenants_new_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:write")),
) -> Response:
    return _template(
        request,
        "admin/tenant_form.html",
        _tenant_form_context(request, None, {}, {}),
    )


async def tenants_new_post(
    request: Request,
    nombre: str = Form(...),
    whatsapp_number: str = Form(...),
    fantasy_name: str | None = Form(None),
    first_name: str | None = Form(None),
    last_name: str | None = Form(None),
    cuil: str | None = Form(None),
    address: str | None = Form(None),
    postal_code: str | None = Form(None),
    phone: str | None = Form(None),
    activo: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    errors: dict[str, str] = {}
    ai_settings, ai_error = await _parse_ai_settings_form(request)
    if ai_error:
        errors["ai_settings"] = ai_error
    cleaned = {
        "nombre": nombre.strip(),
        "whatsapp_number": whatsapp_number.strip(),
        "fantasy_name": _strip_optional(fantasy_name),
        "first_name": _strip_optional(first_name),
        "last_name": _strip_optional(last_name),
        "cuil": _validate_digits(cuil, "cuil", errors),
        "address": _strip_optional(address),
        "postal_code": _validate_digits(postal_code, "postal_code", errors),
        "phone": _validate_digits(phone, "phone", errors),
    }
    if not cleaned["nombre"]:
        errors["nombre"] = "El nombre es obligatorio."
    if not cleaned["whatsapp_number"]:
        errors["whatsapp_number"] = "El numero de WhatsApp es obligatorio."
    if errors:
        cleaned["ai_settings"] = ai_settings
        return _template(
            request,
            "admin/tenant_form.html",
            _tenant_form_context(request, None, errors, cleaned),
        )
    tenant = Tenant(
        nombre=cleaned["nombre"],
        whatsapp_number=cleaned["whatsapp_number"],
        activo=bool(activo),
        fantasy_name=cleaned["fantasy_name"],
        first_name=cleaned["first_name"],
        last_name=cleaned["last_name"],
        cuil=cleaned["cuil"],
        address=cleaned["address"],
        postal_code=cleaned["postal_code"],
        phone=cleaned["phone"],
        ai_settings=ai_settings,
    )
    async with session.begin_nested():
        exists_stmt = select(Tenant.id).where(
            Tenant.whatsapp_number == cleaned["whatsapp_number"],
            Tenant.deleted_at.is_(None),
        )
        exists = await session.execute(exists_stmt)
        if exists.scalar_one_or_none() is not None:
            errors["whatsapp_number"] = "Ese WhatsApp ya esta registrado."
            cleaned["ai_settings"] = ai_settings
            return _template(
                request,
                "admin/tenant_form.html",
                _tenant_form_context(request, None, errors, cleaned),
            )
        session.add(tenant)
        await session.flush()
        await audit_log(
            session,
            request,
            user,
            action="create",
            entity="tenant",
            entity_id=tenant.id,
        )
    add_flash(request, "success", "Tenant creado")
    return RedirectResponse("/admin/tenants", status_code=303)


async def tenants_detail(
    request: Request,
    tenant_id: int,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await get_entity_or_404(session, Tenant, tenant_id)

    consultorios = list(
        (
            await session.execute(
                select(Consultorio).where(
                    Consultorio.tenant_id == tenant.id,
                    Consultorio.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    users = list(
        (
            await session.execute(
                select(User).where(
                    User.tenant_id == tenant.id,
                    User.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return _template(
        request,
        "admin/tenant_detail.html",
        {"tenant": tenant, "consultorios": consultorios, "users": users},
    )


async def tenants_edit_get(
    request: Request,
    tenant_id: int,
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await get_entity_or_404(session, Tenant, tenant_id)
    return _template(
        request,
        "admin/tenant_form.html",
        _tenant_form_context(request, tenant, {}, {}),
    )


async def tenants_edit_post(
    request: Request,
    tenant_id: int,
    nombre: str = Form(...),
    whatsapp_number: str = Form(...),
    fantasy_name: str | None = Form(None),
    first_name: str | None = Form(None),
    last_name: str | None = Form(None),
    cuil: str | None = Form(None),
    address: str | None = Form(None),
    postal_code: str | None = Form(None),
    phone: str | None = Form(None),
    activo: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    errors: dict[str, str] = {}
    async with session.begin_nested():
        tenant = await get_entity_or_404(session, Tenant, tenant_id)
        ai_settings, ai_error = await _parse_ai_settings_form(
            request,
            existing_settings=tenant.ai_settings or {},
        )
        if ai_error:
            errors["ai_settings"] = ai_error
        cleaned = {
            "nombre": nombre.strip(),
            "whatsapp_number": whatsapp_number.strip(),
            "fantasy_name": _strip_optional(fantasy_name),
            "first_name": _strip_optional(first_name),
            "last_name": _strip_optional(last_name),
            "cuil": _validate_digits(cuil, "cuil", errors),
            "address": _strip_optional(address),
            "postal_code": _validate_digits(postal_code, "postal_code", errors),
            "phone": _validate_digits(phone, "phone", errors),
            "ai_settings": ai_settings,
        }
        if not cleaned["nombre"]:
            errors["nombre"] = "El nombre es obligatorio."
        if not cleaned["whatsapp_number"]:
            errors["whatsapp_number"] = "El numero de WhatsApp es obligatorio."
        if errors:
            return _template(
                request,
                "admin/tenant_form.html",
                _tenant_form_context(request, tenant, errors, cleaned),
            )
        if cleaned["whatsapp_number"]:
            exists_stmt = select(Tenant.id).where(
                Tenant.whatsapp_number == cleaned["whatsapp_number"],
                Tenant.id != tenant.id,
                Tenant.deleted_at.is_(None),
            )
            exists = await session.execute(exists_stmt)
            if exists.scalar_one_or_none() is not None:
                errors["whatsapp_number"] = "Ese WhatsApp ya esta registrado."
                return _template(
                    request,
                    "admin/tenant_form.html",
                    _tenant_form_context(request, tenant, errors, cleaned),
                )
        changes = _collect_tenant_profile_changes(tenant, cleaned)
        tenant.nombre = cleaned["nombre"]
        tenant.whatsapp_number = cleaned["whatsapp_number"]
        tenant.fantasy_name = cleaned["fantasy_name"]
        tenant.first_name = cleaned["first_name"]
        tenant.last_name = cleaned["last_name"]
        tenant.cuil = cleaned["cuil"]
        tenant.address = cleaned["address"]
        tenant.postal_code = cleaned["postal_code"]
        tenant.phone = cleaned["phone"]
        tenant.activo = bool(activo)
        tenant.ai_settings = ai_settings
        await audit_log(
            session,
            request,
            user,
            action="update_profile",
            entity="tenant",
            entity_id=tenant.id,
            metadata={**changes, "ai_settings_updated": True},
        )
    add_flash(request, "success", "Tenant actualizado")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


async def tenants_toggle(
    request: Request,
    tenant_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin_nested():
        tenant = await get_entity_or_404(session, Tenant, tenant_id)
        tenant.activo = not tenant.activo
        await audit_log(
            session,
            request,
            user,
            action="toggle",
            entity="tenant",
            entity_id=tenant.id,
            metadata={"activo": tenant.activo},
        )
        if not tenant.activo:
            from app.core.notifications import create_notification

            await create_notification(
                session,
                title="Tenant desactivado",
                message=f"{tenant.nombre} fue desactivado.",
                notif_type="warning",
                tenant_id=None,
            )
    add_flash(request, "success", "Estado actualizado")
    return RedirectResponse("/admin/tenants", status_code=303)


async def tenants_delete(
    request: Request,
    tenant_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin_nested():
        tenant = await get_entity_or_404(session, Tenant, tenant_id)
        tenant.deleted_at = now_ba()
        tenant.deleted_by = user.id
        await audit_log(
            session,
            request,
            user,
            action="delete",
            entity="tenant",
            entity_id=tenant.id,
        )
        from app.core.notifications import create_notification

        await create_notification(
            session,
            title="Tenant eliminado",
            message=f"{tenant.nombre} fue eliminado.",
            notif_type="danger",
            tenant_id=None,
        )
    add_flash(request, "success", "Tenant eliminado")
    return RedirectResponse("/admin/tenants", status_code=303)


async def users_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    show_deleted = request.query_params.get("show_deleted", "")
    stmt = select(User)
    if not show_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    result = await session.execute(stmt.order_by(User.created_at.desc()))
    users = list(result.scalars().all())
    tenants = list(
        (
            await session.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    return _template(
        request,
        "admin/users_list.html",
        {"users": users, "tenants": tenants, "show_deleted": show_deleted},
    )


async def users_new_get(
    request: Request,
    current: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenants = list(
        (
            await session.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    return _template(
        request,
        "admin/user_form.html",
        {"user": None, "tenants": tenants},
    )


async def users_new_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    tenant_id: int | None = Form(None),
    active: str | None = Form(None),
    csrf_token: str = Form(""),
    current: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user_role = UserRole(role)
    assigned_tenant_id = tenant_id if user_role == UserRole.TENANT_ADMIN else None
    form_data = {
        "email": email,
        "role": role,
        "tenant_id": str(tenant_id or ""),
        "active": "1" if active else "",
    }
    new_user = User(
        email=email,
        password_hash=hash_password(password),
        role=user_role.value,
        tenant_id=assigned_tenant_id,
        active=bool(active),
    )
    try:
        async with session.begin_nested():
            session.add(new_user)
            await session.flush()
            await audit_log(
                session,
                request,
                current,
                action="create",
                entity="user",
                entity_id=new_user.id,
            )
    except IntegrityError:
        tenants = list(
            (
                await session.execute(
                    select(Tenant).where(Tenant.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        errors = {"email": "Ya existe un usuario con este email."}
        return _template(
            request,
            "admin/user_form.html",
            {"user": None, "tenants": tenants, "errors": errors, "form_data": form_data},
        )
    add_flash(request, "success", "Usuario creado")
    return RedirectResponse("/admin/users", status_code=303)


async def users_edit_get(
    request: Request,
    user_id: int,
    current: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    user = await get_entity_or_404(session, User, user_id)
    tenants = list(
        (
            await session.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    return _template(
        request,
        "admin/user_form.html",
        {"user": user, "tenants": tenants},
    )


async def users_edit_post(
    request: Request,
    user_id: int,
    email: str = Form(...),
    password: str | None = Form(None),
    role: str = Form(...),
    tenant_id: int | None = Form(None),
    active: str | None = Form(None),
    csrf_token: str = Form(""),
    current: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    form_data = {
        "email": email,
        "role": role,
        "tenant_id": str(tenant_id or ""),
        "active": "1" if active else "",
    }
    try:
        async with session.begin_nested():
            user = await get_entity_or_404(session, User, user_id)
            user.email = email
            user.role = role
            user.active = bool(active)
            user.tenant_id = tenant_id if role == UserRole.TENANT_ADMIN.value else None
            if password:
                user.password_hash = hash_password(password)
            await audit_log(
                session,
                request,
                current,
                action="update",
                entity="user",
                entity_id=user.id,
            )
    except IntegrityError:
        tenants = list(
            (
                await session.execute(
                    select(Tenant).where(Tenant.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        errors = {"email": "Ya existe un usuario con este email."}
        user = await get_entity_or_404(session, User, user_id)
        return _template(
            request,
            "admin/user_form.html",
            {"user": user, "tenants": tenants, "errors": errors, "form_data": form_data},
        )
    add_flash(request, "success", "Usuario actualizado")
    return RedirectResponse("/admin/users", status_code=303)


async def users_toggle(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    current: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        add_flash(request, "error", "Usuario no encontrado")
        return RedirectResponse("/admin/users", status_code=303)
    if user.deleted_at is not None:
        add_flash(request, "error", "Usuario eliminado")
        return RedirectResponse("/admin/users", status_code=303)
    async with session.begin_nested():
        user.active = not user.active
        await audit_log(
            session,
            request,
            current,
            action="toggle",
            entity="user",
            entity_id=user.id,
            metadata={"active": user.active},
        )
        if not user.active:
            from app.core.notifications import create_notification

            await create_notification(
                session,
                title="Usuario desactivado",
                message=f"{user.email} fue desactivado.",
                notif_type="warning",
                tenant_id=user.tenant_id,
            )
    add_flash(request, "success", "Estado actualizado")
    return RedirectResponse("/admin/users", status_code=303)


async def users_delete(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    current: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    if current.id == user_id:
        add_flash(request, "error", "No podes eliminar tu propio usuario")
        return RedirectResponse("/admin/users", status_code=303)
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        add_flash(request, "error", "Usuario no encontrado")
        return RedirectResponse("/admin/users", status_code=303)
    if user.deleted_at is not None:
        add_flash(request, "error", "Usuario ya eliminado")
        return RedirectResponse("/admin/users", status_code=303)
    async with session.begin_nested():
        user.deleted_at = now_ba()
        user.deleted_by = current.id
        await audit_log(
            session,
            request,
            current,
            action="delete",
            entity="user",
            entity_id=user.id,
        )
        from app.core.notifications import create_notification

        await create_notification(
            session,
            title="Usuario eliminado",
            message=f"{user.email} fue eliminado.",
            notif_type="danger",
            tenant_id=user.tenant_id,
        )
    add_flash(request, "success", "Usuario eliminado")
    return RedirectResponse("/admin/users", status_code=303)


async def audit_logs(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant_id = request.query_params.get("tenant_id", "").strip()
    action = request.query_params.get("action", "").strip()
    entity = request.query_params.get("entity", "").strip()

    stmt = select(AuditLog)
    if tenant_id:
        try:
            stmt = stmt.where(AuditLog.tenant_id == int(tenant_id))
        except ValueError:
            tenant_id = ""
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity:
        stmt = stmt.where(AuditLog.entity == entity)

    result = await session.execute(stmt.order_by(desc(AuditLog.created_at)).limit(200))
    logs = list(result.scalars().all())
    log_times = {log.id: _format_local_time(log.created_at) for log in logs}
    tenants = list(
        (
            await session.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    return _template(
        request,
        "admin/audit_logs.html",
        {
            "logs": logs,
            "tenants": tenants,
            "tenant_id": tenant_id,
            "action": action,
            "entity": entity,
            "log_times": log_times,
        },
    )


async def notifications_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    stmt = select(Notification).where(Notification.tenant_id.is_(None))
    result = await session.execute(stmt.order_by(desc(Notification.created_at)))
    items = list(result.scalars().all())
    return _template(
        request,
        "admin/notifications.html",
        {"items": items},
    )


async def notifications_mark_read(
    request: Request,
    notification_id: int,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    stmt = select(Notification).where(
        Notification.id == notification_id,
        Notification.tenant_id.is_(None),
    )
    result = await session.execute(stmt)
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notificacion no encontrada")
    async with session.begin_nested():
        await mark_notification_read(session, notification)
    return RedirectResponse("/admin/notifications", status_code=303)


async def payments_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant_id = request.query_params.get("tenant_id", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    q = request.query_params.get("q", "").strip()

    stmt = (
        select(Payment, Tenant, Paciente, Turno)
        .join(Tenant, Payment.tenant_id == Tenant.id)
        .join(Paciente, Payment.patient_id == Paciente.id)
        .outerjoin(Turno, Payment.appointment_id == Turno.id)
    )
    if tenant_id:
        try:
            stmt = stmt.where(Payment.tenant_id == int(tenant_id))
        except ValueError:
            tenant_id = ""
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
    tenants = list(
        (await session.execute(select(Tenant).where(Tenant.deleted_at.is_(None)))).scalars().all()
    )
    return _template(
        request,
        "admin/payments_list.html",
        {
            "rows": rows,
            "tenants": tenants,
            "tenant_id": tenant_id,
            "status_filter": status_filter,
            "q": q,
        },
    )


async def payment_detail(
    request: Request,
    payment_id: int,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    stmt = (
        select(Payment, Tenant, Paciente, Turno)
        .join(Tenant, Payment.tenant_id == Tenant.id)
        .join(Paciente, Payment.patient_id == Paciente.id)
        .outerjoin(Turno, Payment.appointment_id == Turno.id)
        .where(Payment.id == payment_id)
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
        "admin/payment_detail.html",
        {"row": row, "events": events},
    )


async def notifications_settings(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
) -> Response:
    return _template(
        request,
        "admin/settings_notifications.html",
        {},
    )


async def chat_simulator_get(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    result = await session.execute(select(Tenant).where(Tenant.deleted_at.is_(None)))
    tenants = list(result.scalars().all())
    history = request.session.get("chat_simulator_history", [])
    request.session["chat_simulator_history"] = []
    defaults = request.session.get("chat_simulator_defaults", {})
    return _template(
        request,
        "admin/chat_simulator.html",
        {
            "tenants": tenants,
            "history": history,
            "defaults": defaults,
        },
    )


async def chat_simulator_send(
    request: Request,
    to_number: str = Form(...),
    from_number: str = Form(...),
    message: str = Form(...),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    tenant_service = TenantService(TenantRepository(session))
    tenant = await tenant_service.resolve_by_whatsapp(to_number)
    if tenant is None or not tenant.activo:
        add_flash(request, "error", "No existe un tenant activo con ese numero.")
        return RedirectResponse("/admin/chat-simulator", status_code=303)

    conversation_service = ConversationService(
        session=session,
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
        notification_repo=NotificationRepository(session),
    )

    async with session.begin_nested():
        set_current_tenant_id(tenant.id)
        try:
            reply_text = await conversation_service.process_message(
                tenant=tenant,
                from_phone=from_number,
                body=message,
            )
        finally:
            set_current_tenant_id(None)

    history = request.session.get("chat_simulator_history", [])
    history.append(
        {
            "to_number": to_number,
            "from_number": from_number,
            "message": message,
            "reply": reply_text,
        }
    )
    request.session["chat_simulator_history"] = history[-20:]
    request.session["chat_simulator_defaults"] = {
        "to_number": to_number,
        "from_number": from_number,
    }
    return RedirectResponse("/admin/chat-simulator", status_code=303)


async def chat_simulator_api(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    payload = await request.json()
    csrf_token = request.headers.get("X-CSRF-Token", "")
    validate_csrf(request, csrf_token)

    to_number = (payload.get("to_number") or "").strip()
    from_number = (payload.get("from_number") or "").strip()
    message = (payload.get("message") or "").strip()

    if not to_number or not from_number or not message:
        return JSONResponse({"error": "Completa los campos requeridos."}, status_code=400)

    tenant_service = TenantService(TenantRepository(session))
    tenant = await tenant_service.resolve_by_whatsapp(to_number)
    if tenant is None or not tenant.activo:
        return JSONResponse({"error": "No existe un tenant activo con ese numero."}, status_code=404)

    conversation_service = ConversationService(
        session=session,
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
        notification_repo=NotificationRepository(session),
    )

    async with session.begin_nested():
        set_current_tenant_id(tenant.id)
        try:
            reply_text = await conversation_service.process_message(
                tenant=tenant,
                from_phone=from_number,
                body=message,
            )
        finally:
            set_current_tenant_id(None)

    history = request.session.get("chat_simulator_history", [])
    history.append(
        {
            "to_number": to_number,
            "from_number": from_number,
            "message": message,
            "reply": reply_text,
        }
    )
    request.session["chat_simulator_history"] = history[-20:]
    request.session["chat_simulator_defaults"] = {
        "to_number": to_number,
        "from_number": from_number,
        "patient_id": payload.get("patient_id"),
    }
    return JSONResponse({"reply": reply_text})


async def chat_simulator_patients(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    tenant_id = request.query_params.get("tenant_id", "").strip()
    try:
        tenant_id_int = int(tenant_id)
    except ValueError:
        return JSONResponse({"items": []})

    result = await session.execute(
        select(Paciente)
        .where(Paciente.tenant_id == tenant_id_int, Paciente.deleted_at.is_(None))
        .order_by(Paciente.nombre, Paciente.apellido)
    )
    items = [
        {
            "id": paciente.id,
            "nombre": paciente.nombre,
            "apellido": paciente.apellido,
            "telefono": paciente.telefono,
        }
        for paciente in result.scalars().all()
    ]
    return JSONResponse({"items": items})


async def chat_simulator_reset(
    request: Request,
    to_number: str = Form(""),
    from_number: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    request.session["chat_simulator_history"] = []
    if to_number and from_number:
        tenant_service = TenantService(TenantRepository(session))
        tenant = await tenant_service.resolve_by_whatsapp(to_number)
        if tenant is None:
            add_flash(request, "error", "Tenant no encontrado.")
            return RedirectResponse("/admin/chat-simulator", status_code=303)
        conversacion_repo = ConversacionRepository(session)
        async with session.begin_nested():
            await conversacion_repo.mark_resolved(
                tenant.id,
                from_number,
                resolved_by=user.id,
                close_reason="simulator_reset",
            )
        add_flash(request, "success", "Conversacion reiniciada.")
    else:
        add_flash(request, "success", "Conversacion reiniciada.")
    return RedirectResponse("/admin/chat-simulator", status_code=303)


async def calendars(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    result = await session.execute(select(Tenant).where(Tenant.deleted_at.is_(None)))
    tenants = list(result.scalars().all())
    rows = []
    for tenant in tenants:
        settings = tenant.calendar_settings or {}
        rows.append(
            {
                "tenant": tenant,
                "calendar_id": settings.get("google_calendar_id"),
                "timezone": settings.get("default_timezone"),
                "enabled": bool(settings.get("google_calendar_id")),
            }
        )
    return _template(request, "admin/calendars.html", {"rows": rows})


async def appointments_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("appointment:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant_id = request.query_params.get("tenant_id", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    tipo = request.query_params.get("tipo", "").strip()
    date_str = request.query_params.get("date", "").strip()

    stmt = (
        select(Turno, Tenant, Paciente, Consultorio)
        .join(Consultorio, Turno.consultorio_id == Consultorio.id)
        .join(Tenant, Consultorio.tenant_id == Tenant.id)
        .join(Paciente, Turno.paciente_id == Paciente.id)
        .where(Turno.deleted_at.is_(None), Consultorio.deleted_at.is_(None))
    )
    if tenant_id:
        try:
            stmt = stmt.where(Tenant.id == int(tenant_id))
        except ValueError:
            tenant_id = ""
    if status_filter:
        try:
            stmt = stmt.where(Turno.status == AppointmentStatus(status_filter))
        except ValueError:
            status_filter = ""
    if tipo:
        stmt = stmt.where(Turno.tipo == tipo)
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
    tenants = list(
        (await session.execute(select(Tenant).where(Tenant.deleted_at.is_(None)))).scalars().all()
    )
    return _template(
        request,
        "admin/appointments_list.html",
        {
            "rows": rows,
            "tenants": tenants,
            "tenant_id": tenant_id,
            "status_filter": status_filter,
            "tipo": tipo,
            "date": date_str,
        },
    )


async def conversation_states(
    request: Request,
    user: CurrentUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    context = await tenant_conversation_views._conversation_listing_context(
        request,
        session,
        tenant_id=None,
        scope_prefix="/admin",
        show_tenant=True,
    )
    return _template(request, "tenant/conversation_states.html", context)


async def conversation_state_detail(
    request: Request,
    tenant_id: int,
    telefono: str,
    user: CurrentUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    result = await session.execute(
        select(EstadoConversacion).where(
            EstadoConversacion.tenant_id == tenant_id,
            EstadoConversacion.telefono == telefono,
        )
    )
    state = result.scalar_one_or_none()
    if state is None:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")
    paciente_result = await session.execute(
        select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.deleted_at.is_(None))
    )
    pacientes = list(paciente_result.scalars().all())
    paciente = next(
        (item for item in pacientes if tenant_conversation_views._sanitize_phone(item.telefono) == tenant_conversation_views._sanitize_phone(state.telefono)),
        None,
    )
    tenant = await get_entity_or_404(session, Tenant, tenant_id)
    return _template(
        request,
        "tenant/conversation_state_detail.html",
        {
            "record": state,
            "is_history": False,
            "paciente": paciente,
            "tenant": tenant,
            "contexto_pretty": json.dumps(
                tenant_conversation_views.sanitize_context_for_display(state.contexto_json),
                ensure_ascii=True,
                indent=2,
            ),
            "status": (state.status or "active").lower(),
            "category_label": tenant_conversation_views.CATEGORY_LABELS.get(state.conversation_category or "", "-"),
            "subtype_label": tenant_conversation_views.SUBTYPE_LABELS.get(
                state.conversation_subtype or "", state.conversation_subtype or "-"
            ),
            "operational_category": tenant_conversation_views._resolve_operational_category(
                state.operational_category,
                state.conversation_category,
                state.pending_reason,
                bool(state.requires_human_review),
            ),
            "operational_labels": tenant_conversation_views.OPERATIONAL_CATEGORY_LABELS,
            "whatsapp_link": tenant_conversation_views._build_whatsapp_link(state.telefono),
            "scope_prefix": "/admin",
            "ai_summary": tenant_conversation_views.get_ai_summary_from_context(
                state.contexto_json, mask_sensitive=False
            ),
        },
    )


async def conversation_history_detail(
    request: Request,
    history_id: int,
    user: CurrentUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    history = await session.get(ConversationHistory, history_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")
    paciente = await session.get(Paciente, history.patient_id) if history.patient_id else None
    if paciente is None:
        paciente_result = await session.execute(
            select(Paciente).where(Paciente.tenant_id == history.tenant_id, Paciente.deleted_at.is_(None))
        )
        pacientes = list(paciente_result.scalars().all())
        paciente = next(
            (item for item in pacientes if tenant_conversation_views._sanitize_phone(item.telefono) == tenant_conversation_views._sanitize_phone(history.telefono)),
            None,
        )
    tenant = await get_entity_or_404(session, Tenant, history.tenant_id)
    return _template(
        request,
        "tenant/conversation_state_detail.html",
        {
            "record": history,
            "is_history": True,
            "paciente": paciente,
            "tenant": tenant,
            "contexto_pretty": json.dumps(
                tenant_conversation_views.sanitize_context_for_display(history.contexto_json),
                ensure_ascii=True,
                indent=2,
            ),
            "status": "finished",
            "category_label": tenant_conversation_views.CATEGORY_LABELS.get(history.conversation_category or "", "-"),
            "subtype_label": tenant_conversation_views.SUBTYPE_LABELS.get(
                history.conversation_subtype or "", history.conversation_subtype or "-"
            ),
            "operational_category": tenant_conversation_views._resolve_operational_category(
                history.operational_category,
                history.conversation_category,
                history.pending_reason,
                bool(history.requires_human_review),
            ),
            "operational_labels": tenant_conversation_views.OPERATIONAL_CATEGORY_LABELS,
            "whatsapp_link": tenant_conversation_views._build_whatsapp_link(history.telefono),
            "scope_prefix": "/admin",
            "ai_summary": tenant_conversation_views.get_ai_summary_from_context(
                history.contexto_json, mask_sensitive=False
            ),
        },
    )


async def conversation_state_resolve(
    request: Request,
    tenant_id: int,
    telefono: str,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    repo = ConversacionRepository(session)
    async with session.begin_nested():
        state = await repo.mark_resolved(
            tenant_id,
            telefono,
            resolved_by=user.id,
            close_reason="admin_manual_resolve",
        )
        await audit_log(
            session,
            request,
            user,
            action="conversation_resolved",
            entity="conversation_state",
            metadata={"telefono": telefono, "tenant_id": tenant_id, "pending_reason": getattr(state, "pending_reason", None)},
        )
    add_flash(request, "success", "Conversacion marcada como finalizada")
    return RedirectResponse("/admin/conversation-states?status=resolved", status_code=303)


async def conversation_state_review_update(
    request: Request,
    tenant_id: int,
    telefono: str,
    operational_category: str = Form(""),
    manual_note: str = Form(""),
    ai_corrected_intent: str = Form(""),
    ai_review_note: str = Form(""),
    status_action: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    repo = ConversacionRepository(session)
    operational_category = tenant_conversation_views._normalize_operational_category(operational_category)
    async with session.begin_nested():
        state = await repo.update_operational_review(
            tenant_id,
            telefono,
            operational_category=operational_category,
            manual_note=(manual_note or "").strip() or None,
        )
        if state is None:
            raise HTTPException(status_code=404, detail="Conversacion no encontrada")
        tenant_conversation_views._apply_ai_review(
            state,
            corrected_intent=ai_corrected_intent,
            review_note=ai_review_note,
            reviewed_by=user.id,
        )
        if status_action == "pending":
            await repo.mark_pending_manual(tenant_id, telefono)
            action = "conversation_marked_pending"
        elif status_action == "resolved":
            await repo.mark_resolved(tenant_id, telefono, resolved_by=user.id, close_reason="admin_review_resolve")
            action = "conversation_resolved"
        else:
            action = "conversation_review_updated"
        await audit_log(
            session,
            request,
            user,
            action=action,
            entity="conversation_state",
            metadata={
                "telefono": telefono,
                "tenant_id": tenant_id,
                "operational_category": operational_category,
                "manual_note": (manual_note or "").strip() or None,
                "ai_corrected_intent": (ai_corrected_intent or "").strip() or None,
                "ai_review_note": (ai_review_note or "").strip() or None,
                "status_action": status_action or None,
            },
        )
    add_flash(request, "success", "Revision de conversacion actualizada")
    return RedirectResponse(f"/admin/conversation-states/{tenant_id}/{telefono}", status_code=303)


async def tenant_features_list(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenants = list(
        (
            await session.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.nombre)
            )
        )
        .scalars()
        .all()
    )
    return _template(
        request,
        "admin/tenant_features_list.html",
        {"tenants": tenants},
    )


async def tenant_features_get(
    request: Request,
    tenant_id: int,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    tenant = await get_entity_or_404(session, Tenant, tenant_id)
    service = TenantFeatureService(session)
    async with session.begin_nested():
        await service.sync_tenant_with_registry(tenant.id)
    flags = await service.get_flags(tenant.id)
    return _template(
        request,
        "admin/tenant_features_detail.html",
        {
            "tenant": tenant,
            "features": FEATURE_REGISTRY,
            "flags": flags,
        },
    )


async def tenant_features_post(
    request: Request,
    tenant_id: int,
    csrf_token: str = Form(""),
    action: str | None = Form(None),
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    tenant = await get_entity_or_404(session, Tenant, tenant_id)
    service = TenantFeatureService(session)
    before_flags = await service.get_flags(tenant.id)

    if action == "enable_all":
        next_flags = {key: True for key in FEATURE_REGISTRY}
    elif action == "disable_all":
        next_flags = {key: False for key in FEATURE_REGISTRY}
    else:
        form = await request.form()
        next_flags = {
            key: (form.get(f"feature_{key}") == "1")
            for key in FEATURE_REGISTRY
        }

    async with session.begin_nested():
        after_flags = await service.set_flags(
            tenant_id=tenant.id,
            flags=next_flags,
            updated_by=user.id,
        )
        changed = {
            key: {"before": before_flags.get(key), "after": after_flags.get(key)}
            for key in FEATURE_REGISTRY
            if before_flags.get(key) != after_flags.get(key)
        }
        await audit_log(
            session,
            request,
            user,
            action="tenant_features_updated",
            entity="tenant_features",
            entity_id=tenant.id,
            metadata={"before": before_flags, "after": after_flags, "diff": changed},
            tenant_id=tenant.id,
        )
    add_flash(request, "success", "Features del tenant actualizadas")
    return RedirectResponse(f"/admin/tenant-features/{tenant.id}", status_code=303)

