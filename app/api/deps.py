from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.config import get_settings

security = HTTPBasic()


def admin_basic_auth(
    credentials: HTTPBasicCredentials = Depends(security),
) -> None:
    settings = get_settings()
    if (
        credentials.username != settings.admin_user
        or credentials.password != settings.admin_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales invalidas",
            headers={"WWW-Authenticate": "Basic"},
        )
