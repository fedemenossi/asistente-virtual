from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, token: str | None) -> None:
    expected = request.session.get("csrf_token")
    if not expected or not token or token != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF invalido")
