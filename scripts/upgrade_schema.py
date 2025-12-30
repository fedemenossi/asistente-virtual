from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import engine
from app.models import AuditLog, Notification, Payment, PaymentEvent, Subscription  # noqa: F401
from app.models.base import Base


SOFT_DELETE_COLUMNS = {
    "tenants": ["deleted_at", "deleted_by"],
    "users": ["deleted_at", "deleted_by"],
    "consultorios": ["deleted_at", "deleted_by"],
    "pacientes": ["deleted_at", "deleted_by"],
    "turnos": ["deleted_at", "deleted_by"],
}

EXTRA_COLUMNS = {
    "tenants": {
        "payment_settings": "JSON NULL",
    },
}


async def _column_exists(conn, db_name: str, table: str, column: str) -> bool:
    stmt = text(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = :schema
          AND TABLE_NAME = :table
          AND COLUMN_NAME = :column
        """
    )
    result = await conn.execute(
        stmt, {"schema": db_name, "table": table, "column": column}
    )
    return (result.scalar() or 0) > 0


async def _add_column(conn, table: str, column: str, ddl: str) -> None:
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def upgrade(db_engine: AsyncEngine) -> None:
    url = make_url(db_engine.url)
    db_name = url.database
    if not db_name:
        raise RuntimeError("DATABASE_URL sin nombre de base")

    try:
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            for table, columns in SOFT_DELETE_COLUMNS.items():
                for column in columns:
                    exists = await _column_exists(conn, db_name, table, column)
                    if exists:
                        continue
                    ddl = "DATETIME NULL" if column == "deleted_at" else "INT NULL"
                    await _add_column(conn, table, column, ddl)

            for table, columns in EXTRA_COLUMNS.items():
                for column, ddl in columns.items():
                    exists = await _column_exists(conn, db_name, table, column)
                    if exists:
                        continue
                    await _add_column(conn, table, column, ddl)
    finally:
        await db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(upgrade(engine))
