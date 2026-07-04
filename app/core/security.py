from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from passlib.context import CryptContext
from passlib.hash import pbkdf2_sha256
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_async_session
from app.core.features import feature_defaults
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.tenant_feature_service import TenantFeatureService

_settings = get_settings()
if _settings.app_env.lower() == "test":
    pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
else:
    pwd_context = CryptContext(schemes=["bcrypt", "pbkdf2_sha256"], deprecated="auto")


@dataclass
class CurrentUser:
    id: int
    email: str
    role: UserRole
    tenant_id: int | None


def hash_password(password: str) -> str:
    try:
        return pwd_context.hash(password)
    except Exception:
        return pbkdf2_sha256.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(plain_password, password_hash)
    except Exception:
        if password_hash.startswith("$pbkdf2-sha256$"):
            return pbkdf2_sha256.verify(plain_password, password_hash)
        return False

ROLE_PERMISSIONS: dict[UserRole, set[str]] = {
    UserRole.SUPER_ADMIN: {"*"},
    UserRole.TENANT_ADMIN: {
        "tenant:read",
        "patient:read",
        "patient:write",
        "appointment:read",
        "appointment:write",
        "consultorio:read",
        "consultorio:write",
        "conversation:read",
        "payment:read",
        "payment:write",
        "billing_arca:read",
        "billing_arca:write",
        "settings:write",
        "notification:read",
    },
}


def has_permission(user: CurrentUser, permission: str) -> bool:
    perms = ROLE_PERMISSIONS.get(user.role, set())
    return "*" in perms or permission in perms


def require_permission(permission: str):
    def _checker(user: CurrentUser = Depends(require_login)) -> CurrentUser:
        if not has_permission(user, permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return user

    return _checker


async def _load_user(session: AsyncSession, user_id: int) -> User | None:
    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> CurrentUser | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = await _load_user(session, user_id)
    if user is None or not user.active:
        return None
    if user.tenant_id and user.role == UserRole.TENANT_ADMIN:
        stmt = select(Tenant).where(
            Tenant.id == user.tenant_id,
            Tenant.activo.is_(True),
            Tenant.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        tenant = result.scalar_one_or_none()
        if tenant is None:
            return None
    return CurrentUser(
        id=user.id,
        email=user.email,
        role=UserRole(user.role),
        tenant_id=user.tenant_id,
    )


def require_login(
    user: CurrentUser | None = Depends(get_current_user),
) -> CurrentUser:
    if user is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def require_super_admin(
    user: CurrentUser = Depends(require_login),
) -> CurrentUser:
    if user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


def require_tenant_admin(
    user: CurrentUser = Depends(require_login),
) -> CurrentUser:
    if user.role != UserRole.TENANT_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


async def load_tenant_features(
    request: Request,
    user: CurrentUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_async_session),
) -> CurrentUser:
    flags = await TenantFeatureService(session).get_flags(int(user.tenant_id))
    request.state.tenant_features = flags
    return user


def require_feature(feature_key: str):
    async def _checker(
        request: Request,
        user: CurrentUser = Depends(require_tenant_admin),
        session: AsyncSession = Depends(get_async_session),
    ) -> CurrentUser:
        flags = getattr(request.state, "tenant_features", None)
        if flags is None:
            flags = feature_defaults()
            if user.tenant_id:
                flags = await TenantFeatureService(session).get_flags(int(user.tenant_id))
            request.state.tenant_features = flags
        enabled = bool(flags.get(feature_key, True))
        if not enabled:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Feature deshabilitada")
        return user

    return _checker
