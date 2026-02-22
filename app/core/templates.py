from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.csrf import get_csrf_token
from app.core.timezone import format_ba
from app.core.ui import pop_flashes


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["format_ba"] = format_ba


def base_context(request: Request) -> dict:
    return {
        "request": request,
        "csrf_token": get_csrf_token(request),
        "flashes": pop_flashes(request),
        "notifications": getattr(request.state, "notifications", []),
        "unread_notifications": getattr(request.state, "unread_notifications", 0),
        "tenant_features": getattr(request.state, "tenant_features", {}),
    }
