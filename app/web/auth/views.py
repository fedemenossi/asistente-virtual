from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.csrf import validate_csrf
from app.core.db import get_async_session
from app.core.security import verify_password
from app.core.templates import base_context, templates
from app.core.ui import add_flash
from app.models.user import User


async def login_get(request: Request) -> Response:
    return templates.TemplateResponse(request, "auth/login.html", base_context(request))


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
    await session.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

