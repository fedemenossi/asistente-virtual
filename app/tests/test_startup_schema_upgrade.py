from __future__ import annotations

import asyncio
import importlib
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def test_lifespan_runs_schema_upgrade_outside_test(monkeypatch, tmp_path):
    db_path = tmp_path / "startup.db"
    os.environ["APP_ENV"] = "production"
    os.environ["APP_NAME"] = "asistente-virtual"
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["TWILIO_ACCOUNT_SID"] = "test"
    os.environ["TWILIO_AUTH_TOKEN"] = "test"
    os.environ["TWILIO_WHATSAPP_NUMBER"] = "whatsapp:+100000000"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ADMIN_EMAIL"] = "admin@example.com"
    os.environ["ADMIN_PASSWORD_SEED"] = "change_me"

    import app.core.config as config

    config.get_settings.cache_clear()
    config.get_database_settings.cache_clear()

    import app.core.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    called = {}

    async def _fake_upgrade(engine, *, dispose=True):
        called["dispose"] = dispose

    async def _fake_admin(session):
        return None

    async def _fake_sync(self):
        return 0

    monkeypatch.setattr("scripts.upgrade_schema.upgrade", _fake_upgrade)
    monkeypatch.setattr("app.main.ensure_super_admin", _fake_admin)
    monkeypatch.setattr("app.main.TenantFeatureService.sync_all_tenants_with_registry", _fake_sync)

    async def _run():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_run())
    asyncio.run(db.engine.dispose())

    assert called == {"dispose": False}


def test_lifespan_skips_schema_upgrade_in_test(monkeypatch):
    os.environ["APP_ENV"] = "test"

    import app.core.config as config

    config.get_settings.cache_clear()

    import app.main as main

    importlib.reload(main)

    async def _fail_upgrade(*args, **kwargs):
        raise AssertionError("upgrade no debe correr en tests")

    async def _fake_admin(session):
        return None

    async def _fake_sync(self):
        return 0

    monkeypatch.setattr("scripts.upgrade_schema.upgrade", _fail_upgrade)
    monkeypatch.setattr("app.main.ensure_super_admin", _fake_admin)
    monkeypatch.setattr("app.main.TenantFeatureService.sync_all_tenants_with_registry", _fake_sync)

    async def _run():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_run())


def test_patient_dni_migration_preserves_rows_and_controls_legacy_duplicates(tmp_path):
    from scripts.upgrade_schema import upgrade_patient_dni_identity

    db_path = tmp_path / "patient-identity.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def run_migration():
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE pacientes (
                        id INTEGER PRIMARY KEY,
                        tenant_id INTEGER NOT NULL,
                        dni VARCHAR(32) NOT NULL
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO pacientes (id, tenant_id, dni) VALUES
                        (1, 10, '30.123.456'),
                        (2, 10, '30-123-456'),
                        (3, 20, '30123456'),
                        (4, 10, 'sin-dni')
                    """
                )
            )
            result = await upgrade_patient_dni_identity(conn)
            rows = list(
                (
                    await conn.execute(
                        text("SELECT id, tenant_id, dni, dni_normalized FROM pacientes ORDER BY id")
                    )
                ).mappings().all()
            )
            indexes = list((await conn.execute(text("PRAGMA index_list('pacientes')"))).mappings().all())
            return result, rows, indexes

    result, rows, indexes = asyncio.run(run_migration())
    asyncio.run(engine.dispose())

    assert len(rows) == 4
    assert rows[0]["dni_normalized"] == "30123456"
    assert rows[1]["dni_normalized"] is None
    assert rows[2]["dni_normalized"] == "30123456"
    assert rows[3]["dni_normalized"] is None
    assert result.duplicates_cleared == 1
    assert any(index["name"] == "uq_paciente_tenant_dni" and index["unique"] == 1 for index in indexes)


def test_patient_contact_migration_backfills_many_to_many_links_per_tenant_idempotently(tmp_path):
    from scripts.upgrade_schema import upgrade_patient_contact_links

    db_path = tmp_path / "patient-contacts.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def run_migration():
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE pacientes (
                        id INTEGER PRIMARY KEY,
                        tenant_id INTEGER NOT NULL,
                        telefono VARCHAR(32) NOT NULL,
                        deleted_at DATETIME NULL
                    );
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TABLE whatsapp_contacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id INTEGER NOT NULL,
                        phone_normalized VARCHAR(32) NOT NULL,
                        created_at DATETIME NULL,
                        updated_at DATETIME NULL,
                        UNIQUE (tenant_id, phone_normalized)
                    );
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TABLE patient_contact_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id INTEGER NOT NULL,
                        contact_id INTEGER NOT NULL,
                        patient_id INTEGER NOT NULL,
                        status VARCHAR(20) NOT NULL,
                        created_at DATETIME NULL,
                        updated_at DATETIME NULL,
                        UNIQUE (tenant_id, contact_id, patient_id)
                    );
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO pacientes (id, tenant_id, telefono, deleted_at) VALUES
                        (1, 10, 'whatsapp:+54 9 11 7000-0001', NULL),
                        (2, 10, '+5491170000001', NULL),
                        (3, 20, '5491170000001', NULL),
                        (4, 10, 'sin-telefono', NULL),
                        (5, 10, '5491170000099', '2026-01-01');
                    """
                )
            )
            first = await upgrade_patient_contact_links(conn)
            second = await upgrade_patient_contact_links(conn)
            contacts = list(
                (
                    await conn.execute(
                        text("SELECT id, tenant_id, phone_normalized FROM whatsapp_contacts ORDER BY id")
                    )
                ).mappings().all()
            )
            links = list(
                (
                    await conn.execute(
                        text(
                            "SELECT tenant_id, contact_id, patient_id, status "
                            "FROM patient_contact_links ORDER BY patient_id"
                        )
                    )
                ).mappings().all()
            )
            return first, second, contacts, links

    first, second, contacts, links = asyncio.run(run_migration())
    asyncio.run(engine.dispose())

    assert first.contacts_created == 2
    assert first.links_created == 3
    assert first.skipped_patients == 1
    assert second.contacts_created == 0
    assert second.links_created == 0
    assert len(contacts) == 2
    assert {(row["tenant_id"], row["phone_normalized"]) for row in contacts} == {
        (10, "5491170000001"),
        (20, "5491170000001"),
    }
    assert [row["status"] for row in links] == ["approved", "approved", "approved"]
    assert links[0]["contact_id"] == links[1]["contact_id"]
    assert links[2]["contact_id"] != links[0]["contact_id"]
