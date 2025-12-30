from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import engine
from app.models import (  # noqa: F401
    Consultorio,
    AuditLog,
    EstadoConversacion,
    Notification,
    Paciente,
    Tenant,
    Turno,
    User,
)
from app.models.base import Base


async def init_models(db_engine: AsyncEngine) -> None:
    try:
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(init_models(engine))
