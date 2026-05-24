from __future__ import annotations

import asyncio
import importlib
import os


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
