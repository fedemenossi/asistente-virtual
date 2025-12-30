from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.user import User, UserRole


async def seed_admin() -> None:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(select(User).where(User.email == settings.admin_email))
            user = result.scalar_one_or_none()
            if user is None:
                session.add(
                    User(
                        email=settings.admin_email,
                        password_hash=hash_password(settings.admin_password_seed),
                        role=UserRole.SUPER_ADMIN.value,
                        tenant_id=None,
                        active=True,
                    )
                )


if __name__ == "__main__":
    asyncio.run(seed_admin())
