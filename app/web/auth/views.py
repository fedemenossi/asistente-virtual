from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.csrf import validate_csrf
from app.core.db import get_async_session
from app.core.notifications import mark_notification_read
from app.core.security import CurrentUser, require_login, verify_password
from app.core.config import get_settings
from app.core.templates import base_context, templates
from app.core.ui import add_flash
from app.models.notification import Notification
from app.models.user import User
from app.services.push_service import (
    delete_subscription,
    delete_user_subscriptions,
    save_subscription,
    send_push_to_user,
)


async def login_get(request: Request) -> Response:
    return templates.TemplateResponse(request, "auth/login.html", base_context(request))


async def admin_login_get(request: Request) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


async def admin_login_post(request: Request) -> RedirectResponse:
    return RedirectResponse("/login", status_code=307)


async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    stmt = select(User).where(
        User.email == email,
        User.active.is_(True),
        User.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        add_flash(request, "error", "Credenciales invalidas")
        return RedirectResponse("/login", status_code=303)

    request.session["user_id"] = user.id
    request.session["role"] = user.role
    request.session["tenant_id"] = user.tenant_id
    request.session["user_email"] = user.email

    await audit_log(
        session,
        request,
        None,
        action="login",
        entity="user",
        entity_id=user.id,
        tenant_id=user.tenant_id,
    )
    await session.commit()

    target = "/admin/dashboard" if user.role == "SUPER_ADMIN" else "/t/dashboard"
    return RedirectResponse(target, status_code=303)


async def logout(
    request: Request,
    csrf_token: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user_id = request.session.get("user_id")
    tenant_id = request.session.get("tenant_id")
    await audit_log(
        session,
        request,
        None,
        action="logout",
        entity="user",
        entity_id=user_id,
        tenant_id=tenant_id,
    )
    if user_id:
        await delete_user_subscriptions(session, int(user_id))
    await session.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def push_vapid_key(
    user: CurrentUser = Depends(require_login),
) -> JSONResponse:
    settings = get_settings()
    return JSONResponse({"publicKey": settings.vapid_public_key or ""})


async def push_subscribe(
    request: Request,
    user: CurrentUser = Depends(require_login),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    payload = await request.json()
    csrf_token = request.headers.get("X-CSRF-Token", "")
    validate_csrf(request, csrf_token)
    await save_subscription(session, user.id, user.tenant_id, payload)
    await session.commit()
    return JSONResponse({"ok": True})


async def push_unsubscribe(
    request: Request,
    user: CurrentUser = Depends(require_login),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    payload = await request.json()
    csrf_token = request.headers.get("X-CSRF-Token", "")
    validate_csrf(request, csrf_token)
    endpoint = payload.get("endpoint", "")
    if endpoint:
        await delete_subscription(session, user.id, endpoint)
        await session.commit()
    return JSONResponse({"ok": True})


async def push_test(
    request: Request,
    user: CurrentUser = Depends(require_login),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    csrf_token = request.headers.get("X-CSRF-Token", "")
    validate_csrf(request, csrf_token)
    await send_push_to_user(
        session,
        user_id=user.id,
        title="Notificacion de prueba",
        message="Si ves esto, el push funciona.",
        data={"link": "/t/notifications" if user.tenant_id else "/admin/notifications"},
    )
    return JSONResponse({"ok": True})


async def notifications_mark_read(
    request: Request,
    notification_id: int = Form(...),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_login),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    if user.tenant_id:
        stmt = select(Notification).where(
            Notification.id == notification_id,
            Notification.tenant_id == user.tenant_id,
        )
    else:
        stmt = select(Notification).where(
            Notification.id == notification_id, Notification.tenant_id.is_(None)
        )
    result = await session.execute(stmt)
    notification = result.scalar_one_or_none()
    if notification is None:
        add_flash(request, "error", "Notificacion no encontrada")
    else:
        await mark_notification_read(session, notification)
        await session.commit()
    return RedirectResponse(request.headers.get("Referer", "/"), status_code=303)


async def notifications_mark_all_read(
    request: Request,
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(require_login),
    session: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    if user.tenant_id:
        stmt = select(Notification).where(
            Notification.tenant_id == user.tenant_id,
            Notification.read_at.is_(None),
        )
    else:
        stmt = select(Notification).where(
            Notification.tenant_id.is_(None), Notification.read_at.is_(None)
        )
    result = await session.execute(stmt)
    for notification in result.scalars().all():
        await mark_notification_read(session, notification)
    await session.commit()
    return RedirectResponse(request.headers.get("Referer", "/"), status_code=303)

