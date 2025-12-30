from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.csrf import validate_csrf
from app.core.db import get_async_session
from app.core.notifications import mark_notification_read
from app.core.security import CurrentUser, hash_password, require_permission
from app.core.templates import base_context, templates
from app.core.ui import add_flash
from app.core.tenancy import get_entity_or_404
from app.models.audit_log import AuditLog
from app.models.consultorio import Consultorio
from app.models.conversacion import EstadoConversacion
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import Turno
from app.models.user import User, UserRole
from app.models.notification import Notification


def _template(request: Request, name: str, context: dict) -> Response:
    base = base_context(request)
    base.update(context)
    return templates.TemplateResponse(name, base)


async def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_permission("tenant:read")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    now = datetime.utcnow()
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
    return _template(request, "admin/tenant_form.html", {"tenant": None})


async def tenants_new_post(
    request: Request,
    nombre: str = Form(...),
    whatsapp_number: str = Form(...),
    activo: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    tenant = Tenant(
        nombre=nombre,
        whatsapp_number=whatsapp_number,
        activo=bool(activo),
    )
    async with session.begin():
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
    return _template(request, "admin/tenant_form.html", {"tenant": tenant})


async def tenants_edit_post(
    request: Request,
    tenant_id: int,
    nombre: str = Form(...),
    whatsapp_number: str = Form(...),
    activo: str | None = Form(None),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_permission("tenant:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    async with session.begin():
        tenant = await get_entity_or_404(session, Tenant, tenant_id)
        tenant.nombre = nombre
        tenant.whatsapp_number = whatsapp_number
        tenant.activo = bool(activo)
        await audit_log(
            session,
            request,
            user,
            action="update",
            entity="tenant",
            entity_id=tenant.id,
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
    async with session.begin():
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
    async with session.begin():
        tenant = await get_entity_or_404(session, Tenant, tenant_id)
        tenant.deleted_at = datetime.utcnow()
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
    new_user = User(
        email=email,
        password_hash=hash_password(password),
        role=user_role.value,
        tenant_id=assigned_tenant_id,
        active=bool(active),
    )
    async with session.begin():
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
    async with session.begin():
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
    async with session.begin():
        user = await get_entity_or_404(session, User, user_id)
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
    async with session.begin():
        user = await get_entity_or_404(session, User, user_id)
        user.deleted_at = datetime.utcnow()
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
    async with session.begin():
        await mark_notification_read(session, notification)
    return RedirectResponse("/admin/notifications", status_code=303)

