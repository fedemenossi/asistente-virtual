from __future__ import annotations

from app.core.db import _engine_options


def test_aiomysql_disables_pool_pre_ping() -> None:
    options = _engine_options("mysql+aiomysql://user:pass@host:3306/db")

    assert options["pool_pre_ping"] is False
    assert options["pool_recycle"] == 1800


def test_non_aiomysql_keeps_pool_pre_ping() -> None:
    options = _engine_options("sqlite+aiosqlite:///test.db")

    assert options == {"pool_pre_ping": True}
