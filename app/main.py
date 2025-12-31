from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.api.routes import admin_consultorios, admin_tenants, health, webhook
from app.api.routes import payments_webhook
from app.api.routes import internal
from app.core.bootstrap import ensure_super_admin
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.db import AsyncSessionLocal
from app.core.templates import base_context, templates
from app.core.notifications import count_unread_notifications, get_recent_notifications
from app.core.security import CurrentUser, UserRole
from app.web.admin.router import router as admin_router
from app.web.auth.router import router as auth_router
from app.web.tenant.router import router as tenant_router

settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await ensure_super_admin(session)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 7)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def attach_notifications(request, call_next):
    path = request.url.path
    if path.startswith("/static") or path.startswith("/api") or path.startswith("/webhook"):
        return await call_next(request)
    if "session" not in request.scope:
        return await call_next(request)

    user_id = request.session.get("user_id")
    role = request.session.get("role")
    tenant_id = request.session.get("tenant_id")
    if not user_id or not role:
        return await call_next(request)

    try:
        current = CurrentUser(
            id=int(user_id),
            email=request.session.get("user_email", ""),
            role=UserRole(role),
            tenant_id=tenant_id,
        )
    except Exception:
        return await call_next(request)

    async with AsyncSessionLocal() as session:
        request.state.notifications = await get_recent_notifications(session, current)
        request.state.unread_notifications = await count_unread_notifications(
            session, current
        )
    return await call_next(request)


app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(payments_webhook.router)
app.include_router(internal.router)
app.include_router(admin_tenants.router)
app.include_router(admin_consultorios.router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(tenant_router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 403:
        return templates.TemplateResponse(
            request,
            "errors/403.html",
            base_context(request),
            status_code=403,
        )
    if exc.status_code == 404:
        return templates.TemplateResponse(
            request,
            "errors/404.html",
            base_context(request),
            status_code=404,
        )
    return HTMLResponse(content=str(exc.detail), status_code=exc.status_code)


@app.exception_handler(Exception)
async def internal_exception_handler(request: Request, exc: Exception):
    return templates.TemplateResponse(
        request,
        "errors/500.html",
        base_context(request),
        status_code=500,
    )
