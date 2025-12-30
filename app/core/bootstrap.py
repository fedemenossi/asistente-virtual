from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.user import User, UserRole


async def ensure_super_admin(session: AsyncSession) -> None:
    settings = get_settings()
    if settings.app_env.lower() != "development":
        return

    stmt = select(User).where(User.email == settings.admin_email)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        admin = User(
            email=settings.admin_email,
            password_hash=hash_password(settings.admin_password_seed),
            role=UserRole.SUPER_ADMIN.value,
            tenant_id=None,
            active=True,
        )
        session.add(admin)
